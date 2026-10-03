import pytest

from zoho_inventory_connector.errors import NotFoundError, ValidationError
from zoho_inventory_connector.service import customer_summary, fulfilment_state

from .conftest import API, page, so_row

PAY = "pay_TjJrIOvtskFJU8"


@pytest.mark.parametrize("status,shipped,expected", [
    ("draft", "", "draft"),
    ("void", "pending", "void"),
    ("confirmed", "pending", "awaiting_shipment"),
    ("partially_shipped", "partially_shipped", "partially_shipped"),
    ("shipped", "shipped", "shipped"),
    ("fulfilled", "fulfilled", "delivered"),
])
def test_fulfilment_state_mapping(status, shipped, expected):
    assert fulfilment_state({"status": status, "shipped_status": shipped}) == expected


async def test_find_order_by_payment_id_returns_detail(service, zoho):
    zoho.get(f"{API}/salesorders").respond(json=page("salesorders", [so_row("SO-1", PAY)]))
    zoho.get(f"{API}/salesorders/id-SO-1").respond(json={"code": 0, "salesorder": {
        **so_row("SO-1", PAY),
        "line_items": [{"sku": "MUG", "quantity": 1, "quantity_shipped": 0}],
        "packages": [],
    }})
    result = await service.find_order_by_payment_id(PAY)
    assert result["found"] is True
    order = result["orders"][0]
    assert order["razorpay_payment_id"] == PAY
    assert order["fulfilment_state"] == "awaiting_shipment"
    assert order["line_items"][0]["sku"] == "MUG"


async def test_find_order_rechecks_exact_match(service, zoho):
    # Defends against a Zoho behaviour change returning near matches.
    zoho.get(f"{API}/salesorders").respond(json=page("salesorders", [so_row("SO-9", PAY + "X")]))
    result = await service.find_order_by_payment_id(PAY)
    assert result["found"] is False
    assert "Razorpay" in result["guidance"]


async def test_find_order_flags_duplicates(service, zoho):
    zoho.get(f"{API}/salesorders").respond(json=page("salesorders", [so_row("SO-1", PAY), so_row("SO-2", PAY)]))
    zoho.get(url__regex=rf"{API}/salesorders/id-SO-\d").respond(
        json={"code": 0, "salesorder": {**so_row("SO-1", PAY), "line_items": [], "packages": []}}
    )
    result = await service.find_order_by_payment_id(PAY)
    assert "duplicate" in result["warning"]


@pytest.mark.parametrize("bad", ["", "pay_short", "order_TjJrIOvtskFJU8", "pay_TjJrIOvtskFJU8; DROP"])
async def test_invalid_payment_id_never_calls_zoho(service, zoho, bad):
    route = zoho.get(f"{API}/salesorders")
    with pytest.raises(ValidationError):
        await service.find_order_by_payment_id(bad)
    assert not route.called


async def test_paid_unshipped_filters_and_sorts_oldest_first(service, zoho):
    zoho.get(f"{API}/salesorders").respond(json=page("salesorders", [
        so_row("SO-1", PAY, day="2026-10-02"),                                   # 1 day, unshipped
        so_row("SO-2", "pay_AAAAAAAAAAAAAA", day="2026-09-24"),                  # 9 days, unshipped
        so_row("SO-3", "pay_BBBBBBBBBBBBBB", status="shipped", shipped="shipped"),  # shipped
        so_row("SO-4", None, day="2026-09-20"),                                  # no payment id
        so_row("SO-5", "PO-778", day="2026-09-20"),                              # non-Razorpay reference
    ]))
    result = await service.list_paid_unshipped_orders(min_days_waiting=0)
    assert [o["salesorder_number"] for o in result["orders"]] == ["SO-2", "SO-1"]
    assert result["orders"][0]["days_waiting"] == 9
    only_old = await service.list_paid_unshipped_orders(min_days_waiting=3)
    assert [o["salesorder_number"] for o in only_old["orders"]] == ["SO-2"]


async def test_list_orders_state_filter_is_client_side(service, zoho):
    route = zoho.get(f"{API}/salesorders").respond(json=page("salesorders", [
        so_row("SO-1", PAY, status="shipped", shipped="shipped"),
        so_row("SO-2", PAY),
    ]))
    result = await service.list_sales_orders(fulfilment_state_filter="shipped")
    assert [o["salesorder_number"] for o in result["orders"]] == ["SO-1"]
    assert "filter_by" not in route.calls.last.request.url.params


async def test_list_orders_maps_date_filters(service, zoho):
    route = zoho.get(f"{API}/salesorders").respond(json=page("salesorders", []))
    await service.list_sales_orders(date_from="2026-09-01", date_to="2026-09-30")
    params = route.calls.last.request.url.params
    assert params["date_start"] == "2026-09-01" and params["date_end"] == "2026-09-30"


@pytest.mark.parametrize("kwargs", [
    {"date_from": "01-09-2026"},
    {"date_from": "2026-02-30"},
    {"date_from": "2026-10-01", "date_to": "2026-09-01"},
    {"per_page": 500},
    {"page": 0},
    {"fulfilment_state_filter": "lost"},
])
async def test_list_orders_validation(service, kwargs):
    with pytest.raises(ValidationError):
        await service.list_sales_orders(**kwargs)


async def test_get_order_requires_exactly_one_identifier(service):
    with pytest.raises(ValidationError):
        await service.get_sales_order()
    with pytest.raises(ValidationError):
        await service.get_sales_order(salesorder_id="1", salesorder_number="SO-1")


async def test_get_order_by_number_uses_exact_number(service, zoho):
    zoho.get(f"{API}/salesorders").respond(json=page("salesorders", [so_row("SO-10", None), so_row("SO-1", None)]))
    detail = zoho.get(f"{API}/salesorders/id-SO-1").respond(
        json={"code": 0, "salesorder": {**so_row("SO-1", None), "line_items": [], "packages": []}}
    )
    result = await service.get_sales_order(salesorder_number="SO-1")
    assert result["salesorder_number"] == "SO-1"
    assert detail.called


async def test_get_order_unknown_number(service, zoho):
    zoho.get(f"{API}/salesorders").respond(json=page("salesorders", []))
    with pytest.raises(NotFoundError):
        await service.get_sales_order(salesorder_number="SO-404")


async def test_fulfilment_status_reports_tracking_evidence(service, zoho):
    zoho.get(f"{API}/salesorders/id-SO-4").respond(json={"code": 0, "salesorder": {
        **so_row("SO-4", PAY, status="fulfilled", shipped="fulfilled", day="2026-09-26"),
        "line_items": [],
        "packages": [{"package_number": "PKG-4", "status": "delivered", "shipment_number": "SHP-4",
                      "shipment_status": "delivered", "carrier": "Demo", "tracking_number": "T4",
                      "shipment_date": "2026-09-26", "shipment_order": {"shipment_delivered_date": "2026-09-28"}}],
    }})
    result = await service.get_fulfilment_status(salesorder_id="id-SO-4")
    assert result["fulfilment_state"] == "delivered"
    assert result["days_since_order"] == 7
    assert result["evidence_available"] is True
    assert result["packages"][0]["delivered_date"] == "2026-09-28"


async def test_check_stock_reports_shortfall_and_unknown(service, zoho):
    def items_by_sku(request):
        sku = request.url.params["sku"]
        rows = [{"item_id": "i-jkt", "sku": "JKT-L"}] if sku == "JKT-L" else []
        return __import__("httpx").Response(200, json=page("items", rows))

    zoho.get(f"{API}/items").mock(side_effect=items_by_sku)
    zoho.get(f"{API}/items/i-jkt").respond(json={"code": 0, "item": {
        "item_id": "i-jkt", "sku": "JKT-L", "name": "Denim Jacket",
        "actual_available_stock": 0, "actual_committed_stock": 1,
        "actual_available_for_sale_stock": -1, "stock_on_hand": 0, "track_inventory": True,
    }})
    result = await service.check_stock(["JKT-L", "NOPE", "JKT-L"])
    assert result["unknown_skus"] == ["NOPE"]
    assert len(result["stock"]) == 1  # duplicates collapsed
    assert result["stock"][0]["shortfall"] == 1


async def test_check_stock_limits(service):
    with pytest.raises(ValidationError):
        await service.check_stock([])
    with pytest.raises(ValidationError):
        await service.check_stock([f"S{i}" for i in range(11)])


def test_customer_projection_drops_sensitive_fields():
    out = customer_summary({
        "contact_id": "c1", "contact_name": "Asha", "email": "a@example.com",
        "phone": "999", "mobile": "888", "pan_no": "ABCDE1234F", "billing_address": {"x": 1},
    })
    assert set(out) == {"customer_id", "name", "company_name", "email", "status", "outstanding_receivable", "currency"}


async def test_search_customers_prefers_email(service, zoho):
    route = zoho.get(f"{API}/contacts").respond(json=page("contacts", []))
    await service.search_customers(query="asha", email="a@example.com")
    params = route.calls.last.request.url.params
    assert params["email"] == "a@example.com" and "search_text" not in params
