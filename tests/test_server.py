import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from zoho_inventory_connector import server
from zoho_inventory_connector.errors import DailyBudgetExhaustedError

from .conftest import API, page, so_row


@pytest.fixture(autouse=True)
def use_test_service(service, monkeypatch):
    monkeypatch.setattr(server, "_service", service)


async def test_every_tool_is_annotated_read_only():
    tools = await server.mcp.list_tools()
    assert len(tools) == 12
    for t in tools:
        assert t.annotations.read_only_hint is True
        assert t.annotations.destructive_hint is False
        assert t.description and len(t.description) > 40


async def test_tool_returns_structured_result(zoho):
    zoho.get(f"{API}/salesorders").respond(json=page("salesorders", [so_row("SO-1", "pay_AAAAAAAAAAAAAA")]))
    result = await server.mcp.call_tool("list_sales_orders", {"per_page": 5})
    payload = json.loads(result.content[0].text)
    assert payload["orders"][0]["razorpay_payment_id"] == "pay_AAAAAAAAAAAAAA"


def _error_payload(exc: ToolError) -> dict:
    text = str(exc)
    return json.loads(text[text.index("{"):])


async def test_app_errors_become_structured_tool_errors():
    with pytest.raises(ToolError) as err:
        await server.mcp.call_tool("find_order_by_payment_id", {"payment_id": "bad"})
    payload = _error_payload(err.value)
    assert payload["error"] == "VALIDATION_ERROR"
    assert payload["retryable"] is False
    assert payload["correlation_id"]


async def test_budget_exhaustion_tells_agent_to_stop(service, monkeypatch):
    def exhausted():
        raise DailyBudgetExhaustedError(900, 3600)

    monkeypatch.setattr(service.client.budget, "consume", exhausted)
    with pytest.raises(ToolError) as err:
        await server.mcp.call_tool("connection_status", {})
    payload = _error_payload(err.value)
    assert payload["error"] == "DAILY_BUDGET_EXHAUSTED"
    assert "Stop" in payload["hint"]


async def test_unexpected_errors_do_not_leak_internals(service, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(service, "connection_status", boom)
    with pytest.raises(ToolError) as err:
        await server.mcp.call_tool("connection_status", {})
    assert "secret internal detail" not in str(err.value)
    assert _error_payload(err.value)["error"] == "INTERNAL_ERROR"
