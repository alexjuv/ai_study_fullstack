from __future__ import annotations

import ast
import asyncio
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

import tool_governance_demo as demo

SOURCE = "ACC-A-123456"
TARGET = "ACC-A-654321"


@pytest.fixture
def context():
    return demo.base_context(
        permissions=frozenset({"transfer:execute"}),
        allowed_tools=frozenset({"transfer"}),
    )


@pytest.fixture
def arguments():
    return {"from_account": SOURCE, "to_account": TARGET, "amount": 1.0}


@pytest.fixture
def harness():
    snapshot = dict(demo.ACCOUNTS)
    try:
        yield demo.build_runtime()
    finally:
        demo.ACCOUNTS.clear()
        demo.ACCOUNTS.update(snapshot)


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"amount": -0.01},
    {"amount": -0.0},
    {"amount": float("nan")},
    {"amount": float("inf")},
    {"amount": float("-inf")},
    {"amount": 100_000.01},
    {"amount": 0.001},
    {"amount": 1.0000000001},
    {"amount": True},
    {"amount": "1.00"},
    {"amount": None},
    {"from_account": "ACC-A-123456\n"},
    {"from_account": "ACC-a-123456"},
    {"to_account": "ACC-A-１２３４５６"},
    {"to_account": SOURCE},
    {"tenant_id": "tenant_b"},
])
async def test_transfer_rejects_boundary_arguments_without_mutation(harness, context, arguments, overrides):
    runtime, _, audit = harness
    before = dict(demo.ACCOUNTS)
    result = await runtime.invoke(demo.ToolCall("invalid", "transfer", arguments | overrides), context)
    assert result.code == "INVALID_ARGUMENT"
    assert result.action is demo.DecisionAction.DENY
    assert demo.ACCOUNTS == before
    assert [(record.phase, record.code) for record in audit.records] == [("decision", "INVALID_ARGUMENT")]


@pytest.mark.asyncio
@pytest.mark.parametrize("amount,code", [
    (0.01, "APPROVAL_REQUIRED"),
    (50_000.0, "APPROVAL_REQUIRED"),
    (50_000.01, "EXCEED_LIMIT"),
    (80_000.0, "EXCEED_LIMIT"),
    (80_000.01, "APPROVAL_REQUIRED"),
    (100_000.0, "APPROVAL_REQUIRED"),
])
async def test_transfer_amount_interval_endpoints(harness, context, arguments, amount, code):
    runtime, _, _ = harness
    before = dict(demo.ACCOUNTS)
    result = await runtime.invoke(demo.ToolCall("endpoint", "transfer", arguments | {"amount": amount}), context)
    assert result.code == code
    assert demo.ACCOUNTS == before


@pytest.mark.asyncio
async def test_transfer_limit_precedes_insufficient_balance(harness, context, arguments):
    runtime, _, _ = harness
    demo.ACCOUNTS[(context.tenant_id, SOURCE)] = 0.0
    result = await runtime.invoke(demo.ToolCall("limit_first", "transfer", arguments | {"amount": 60_000.0}), context)
    assert result.code == "EXCEED_LIMIT"


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"from_account": "ACC-A-000000"},
    {"to_account": "ACC-A-000000"},
    {"from_account": "ACC-B-111111"},
    {"to_account": "ACC-B-111111"},
])
async def test_transfer_missing_or_foreign_account_never_debits(harness, context, arguments, overrides):
    runtime, approvals, _ = harness
    arguments |= overrides
    before = dict(demo.ACCOUNTS)
    approvals.approve("approved", context, "transfer", arguments)
    result = await runtime.invoke(demo.ToolCall("missing", "transfer", arguments), replace(context, approval_id="approved"))
    assert result.code == "ACCOUNT_NOT_FOUND"
    assert result.action is demo.DecisionAction.DENY
    assert demo.ACCOUNTS == before


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [0.01, 20_000.0, 50_000.0])
async def test_transfer_can_spend_exact_balance(harness, context, arguments, amount):
    runtime, approvals, _ = harness
    demo.ACCOUNTS[(context.tenant_id, SOURCE)] = amount
    arguments["amount"] = amount
    approvals.approve("exact", context, "transfer", arguments)
    result = await runtime.invoke(demo.ToolCall("exact", "transfer", arguments), replace(context, approval_id="exact"))
    assert result.ok
    assert demo.ACCOUNTS[(context.tenant_id, SOURCE)] == 0.0
    assert demo.ACCOUNTS[(context.tenant_id, TARGET)] == 5_000.0 + amount


@pytest.mark.asyncio
async def test_transfer_repeated_small_amounts_preserve_money(harness, context, arguments):
    runtime, approvals, _ = harness
    demo.ACCOUNTS[(context.tenant_id, SOURCE)] = 1.0
    demo.ACCOUNTS[(context.tenant_id, TARGET)] = 0.0
    arguments["amount"] = 0.1
    for index in range(10):
        approval_id = f"cent-{index}"
        approvals.approve(approval_id, context, "transfer", arguments)
        result = await runtime.invoke(demo.ToolCall(approval_id, "transfer", arguments), replace(context, approval_id=approval_id))
        assert result.ok
    assert demo.ACCOUNTS[(context.tenant_id, SOURCE)] == 0.0
    assert demo.ACCOUNTS[(context.tenant_id, TARGET)] == 1.0


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [1, 1.0])
async def test_transfer_approval_matches_integer_and_float_amount(harness, context, arguments, amount):
    runtime, approvals, _ = harness
    arguments["amount"] = amount
    approvals.approve("numeric", context, "transfer", arguments)
    result = await runtime.invoke(demo.ToolCall("numeric", "transfer", arguments), replace(context, approval_id="numeric"))
    assert result.ok


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"user_id": "other_user"},
    {"tenant_id": "tenant_b"},
    {"to_account": "ACC-A-888888"},
])
async def test_transfer_approval_cannot_change_identity_or_recipient(harness, context, arguments, overrides):
    runtime, approvals, _ = harness
    demo.ACCOUNTS[("tenant_b", SOURCE)] = 100_000.0
    demo.ACCOUNTS[("tenant_b", TARGET)] = 5_000.0
    approvals.approve("bound", context, "transfer", arguments)
    before = dict(demo.ACCOUNTS)
    call_args = arguments | ({"to_account": overrides["to_account"]} if "to_account" in overrides else {})
    ctx_changes = {key: value for key, value in overrides.items() if key != "to_account"}
    result = await runtime.invoke(demo.ToolCall("bound", "transfer", call_args), replace(context, approval_id="bound", **ctx_changes))
    assert result.action is demo.DecisionAction.CONFIRM
    assert demo.ACCOUNTS == before


@pytest.mark.asyncio
async def test_transfer_expired_approval_is_rejected(harness, context, arguments):
    runtime, approvals, _ = harness
    approvals.approve("expired", context, "transfer", arguments, ttl_seconds=-1)
    before = dict(demo.ACCOUNTS)
    result = await runtime.invoke(demo.ToolCall("expired", "transfer", arguments), replace(context, approval_id="expired"))
    assert result.action is demo.DecisionAction.CONFIRM
    assert demo.ACCOUNTS == before


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,action", [
    (demo.PermissionMode.BYPASS_PERMISSIONS, demo.DecisionAction.CONFIRM),
    (demo.PermissionMode.DONT_ASK, demo.DecisionAction.DENY),
])
async def test_transfer_modes_cannot_skip_high_risk_approval(harness, context, arguments, mode, action):
    runtime, _, _ = harness
    before = dict(demo.ACCOUNTS)
    result = await runtime.invoke(demo.ToolCall("mode", "transfer", arguments), replace(context, mode=mode))
    assert result.action is action
    assert result.code == "APPROVAL_REQUIRED"
    assert demo.ACCOUNTS == before


@pytest.mark.asyncio
async def test_transfer_parallel_calls_cannot_overdraw(harness, context, arguments):
    runtime, approvals, _ = harness
    demo.ACCOUNTS[(context.tenant_id, SOURCE)] = 5.0
    before_target = demo.ACCOUNTS[(context.tenant_id, TARGET)]
    calls = []
    for index in range(10):
        approval_id = f"parallel-{index}"
        approvals.approve(approval_id, context, "transfer", arguments)
        calls.append(runtime.invoke(demo.ToolCall(approval_id, "transfer", arguments), replace(context, approval_id=approval_id)))
    results = await asyncio.gather(*calls)
    assert sum(result.ok for result in results) == 5
    assert sum(result.code == "INSUFFICIENT_BALANCE" for result in results) == 5
    assert demo.ACCOUNTS[(context.tenant_id, SOURCE)] == 0.0
    assert demo.ACCOUNTS[(context.tenant_id, TARGET)] == before_target + 5.0


@pytest.mark.asyncio
async def test_transfer_parallel_replay_consumes_approval_once(harness, context, arguments):
    runtime, approvals, _ = harness
    approvals.approve("once", context, "transfer", arguments)
    approved = replace(context, approval_id="once")
    results = await asyncio.gather(*(runtime.invoke(demo.ToolCall(str(i), "transfer", arguments), approved) for i in range(10)))
    assert sum(result.ok for result in results) == 1
    assert sum(result.action is demo.DecisionAction.CONFIRM for result in results) == 9
    assert demo.ACCOUNTS[(context.tenant_id, SOURCE)] == 99_999.0


@pytest.mark.asyncio
async def test_transfer_cancellation_during_delay_keeps_balances(harness, context, arguments):
    runtime, approvals, _ = harness
    arguments["amount"] = 90_000.0
    approvals.approve("cancel", context, "transfer", arguments)
    before = dict(demo.ACCOUNTS)
    task = asyncio.create_task(runtime.invoke(demo.ToolCall("cancel", "transfer", arguments), replace(context, approval_id="cancel")))
    await asyncio.sleep(0.01)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert demo.ACCOUNTS == before
    replay = await runtime.invoke(demo.ToolCall("retry", "transfer", arguments), replace(context, approval_id="cancel"))
    assert replay.action is demo.DecisionAction.CONFIRM


@pytest.mark.asyncio
async def test_transfer_model_message_and_audit_do_not_leak_accounts(harness, context, arguments):
    runtime, approvals, audit = harness
    approvals.approve("safe", context, "transfer", arguments)
    result = await runtime.invoke(demo.ToolCall("safe", "transfer", arguments), replace(context, approval_id="safe"))
    payload = json.loads(result.to_tool_message()["content"])
    assert payload["content"]["from"] == "ACC-A-****3456"
    assert payload["content"]["to"] == "ACC-A-****4321"
    encoded = json.dumps([asdict(record) for record in audit.records])
    assert SOURCE not in encoded and TARGET not in encoded
    assert {record.tool_call_id for record in audit.records} == {"safe"}


def test_permission_engine_is_identical_to_exercise():
    root = Path(__file__).resolve().parents[1]
    texts = [path.read_text(encoding="utf-8") for path in (
        root / "originals" / "tool_governance_demo-无答案.py",
        root / "tool_governance_demo.py",
    )]
    classes = [ast.get_source_segment(text, next(node for node in ast.parse(text).body if isinstance(node, ast.ClassDef) and node.name == "PermissionEngine")) for text in texts]
    assert classes[0] == classes[1]


@pytest.mark.asyncio
async def test_transfer_deny_rule_beats_allow_bypass_and_approval(harness, context, arguments):
    _, approvals, audit = harness
    runtime, _, _ = demo.build_runtime(
        approvals=approvals,
        audit=audit,
        rules=(demo.PermissionRule("allow", "transfer"), demo.PermissionRule("deny", "transfer", SOURCE)),
    )
    approvals.approve("denied", context, "transfer", arguments)
    before = dict(demo.ACCOUNTS)
    result = await runtime.invoke(
        demo.ToolCall("denied", "transfer", arguments),
        replace(context, mode=demo.PermissionMode.BYPASS_PERMISSIONS, approval_id="denied"),
    )
    assert result.code == "DENY_RULE"
    assert demo.ACCOUNTS == before


@pytest.mark.asyncio
async def test_redaction_protects_accounts_in_chinese_and_nested_values(harness, context, monkeypatch):
    runtime, _, _ = harness
    monkeypatch.setitem(demo.ORDERS[("tenant_a", "ord_1001")], "note", {
        "accounts": [f"转出账户{SOURCE}，转入账户{TARGET}。"],
        "email": "alice@example.com",
        "token": "private-token",
    })
    result = await runtime.invoke(
        demo.ToolCall("nested", "get_order", {"order_id": "ord_1001"}),
        replace(context, permissions=frozenset({"order:read"}), allowed_tools=frozenset({"get_order"})),
    )
    assert result.ok
    assert result.content["note"] == {
        "accounts": ["转出账户ACC-A-****3456，转入账户ACC-A-****4321。"],
        "email": "***@***",
        "token": "***",
    }
