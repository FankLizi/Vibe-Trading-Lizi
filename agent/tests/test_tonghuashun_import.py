"""同花顺 holdings and delivery-note imports remain local and fail closed."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import portfolio_routes
from src.api.security import require_auth, require_settings_write_auth
from src.portfolio.config import PortfolioSettingsStore
from src.portfolio.fx import FxQuote
from src.portfolio.imports import PortfolioImportStore
from src.portfolio.service import PortfolioService
from src.portfolio.store import PortfolioStore
from src.portfolio.tonghuashun import TonghuashunFormatError, parse_holdings
from src.tools.trade_journal_parsers import parse_file

NOW = datetime(2026, 10, 25, 16, 0, tzinfo=timezone.utc)
HOLDINGS_HEADER = "\t".join(
    [
        "操作",
        "序号",
        "证券代码",
        "证券名称",
        "股票余额",
        "可用余额",
        "冻结数量",
        "成本价",
        "市价",
        "盈亏",
        "盈亏比例(%)",
        "当日盈亏",
        "当日盈亏比(%)",
        "市值",
        "仓位占比(%)",
        "当日买入",
        "当日卖出",
        "交易市场",
        "持股天数",
    ]
)


def holdings_row(
    *,
    code: str,
    name: str,
    quantity: str,
    cost: str,
    price: str,
    market_value: str,
    market: str,
) -> str:
    return "\t".join(
        [
            "",
            "1",
            code,
            name,
            quantity,
            quantity,
            "0",
            cost,
            price,
            "",
            "",
            "",
            "",
            market_value,
            "",
            "0",
            "0",
            market,
            "10",
        ]
    )


def holdings_bytes() -> bytes:
    text = "\n".join(
        [
            HOLDINGS_HEADER,
            holdings_row(
                code="515450",
                name="红利低波50",
                quantity="1000",
                cost="1.300",
                price="1.400",
                market_value="1400.000",
                market="上海Ａ股",
            ),
            holdings_row(
                code="000400",
                name="许继电气",
                quantity="200",
                cost="30.000",
                price="25.000",
                market_value="5000.000",
                market="深圳Ａ股",
            ),
        ]
    )
    return text.encode("gb18030")


def test_holdings_parser_maps_a_share_symbols_and_exchange_traded_funds():
    positions = parse_holdings(holdings_bytes())
    assert [row["symbol"] for row in positions] == ["515450.SH", "000400.SZ"]
    assert [row["asset_type"] for row in positions] == ["etf", "stock"]
    assert all(row["currency"] == "CNY" for row in positions)
    assert all(row["priced"] for row in positions)


def test_holdings_parser_refuses_inconsistent_export_valuation():
    content = holdings_bytes().replace(b"5000.000", b"4999.000")
    with pytest.raises(TonghuashunFormatError, match="does not match"):
        parse_holdings(content)


def service(tmp_path: Path, imported: dict) -> PortfolioService:
    settings = PortfolioSettingsStore(tmp_path / "portfolio.json")
    settings.save(
        {
            "display_currency": "CNY",
            "sources": [
                {
                    "connection_id": imported["id"],
                    "source_kind": "tonghuashun",
                    "label": "开源证券 · 同花顺",
                }
            ],
        }
    )

    def no_broker(*args, **kwargs):
        pytest.fail("file imports must never call a broker")

    return PortfolioService(
        PortfolioStore(tmp_path / "snapshots.sqlite3"),
        settings_store=settings,
        get_account=no_broker,
        get_positions=no_broker,
        get_quote=no_broker,
        fx_fetcher=lambda: FxQuote(
            {
                "USD": Decimal(1),
                "CNY": Decimal(7),
                "HKD": Decimal(8),
                "EUR": Decimal("0.9"),
            },
            NOW.isoformat(),
            NOW.date().isoformat(),
        ),
    )


def test_tonghuashun_import_refresh_and_risk_inputs(tmp_path):
    settings = PortfolioSettingsStore(tmp_path / "portfolio.json")
    imported = settings.import_store.create_tonghuashun(
        content=holdings_bytes(),
        label="开源证券 · 同花顺",
        source_mtime=(NOW - timedelta(minutes=5)).isoformat(),
        expected_positions=2,
        max_age_days=7,
        now=NOW,
    )
    instance = service(tmp_path, imported)
    snapshot = instance.refresh()
    assert snapshot["complete"] is True
    assert snapshot["totals"]["display"] == pytest.approx(6400)
    assert snapshot["totals"]["usd"] == pytest.approx(6400 / 7, rel=1e-6)
    assert snapshot["accounts"][0]["source_kind"] == "tonghuashun"
    assert snapshot["accounts"][0]["broker"] == "tonghuashun"
    assert {row["symbol"] for row in snapshot["positions"]} == {"515450.SH", "000400.SZ"}
    context = instance.analysis_context(snapshot["snapshot_id"])
    assert set(context["risk_xray_args"]["symbols"]) == {"515450.SH", "000400.SZ"}
    assert any("tonghuashun holdings file" in warning for warning in snapshot["warnings"])


def test_web_holdings_import_auto_detects_tonghuashun(tmp_path, monkeypatch):
    settings = PortfolioSettingsStore(tmp_path / "portfolio.json")
    instance = PortfolioService(
        PortfolioStore(tmp_path / "snapshots.sqlite3"),
        settings_store=settings,
        get_account=lambda *a, **k: pytest.fail("unexpected broker read"),
        get_positions=lambda *a, **k: pytest.fail("unexpected broker read"),
        get_quote=lambda *a, **k: pytest.fail("unexpected broker read"),
        fx_fetcher=lambda: (_ for _ in ()).throw(RuntimeError("no network in test")),
    )
    monkeypatch.setattr(portfolio_routes, "PortfolioService", lambda **kwargs: instance)
    app = FastAPI()
    app.dependency_overrides[require_auth] = lambda: None
    app.dependency_overrides[require_settings_write_auth] = lambda: None
    portfolio_routes.register_portfolio_routes(app)
    client = TestClient(app)
    response = client.post(
        "/api/portfolio/imports/holdings",
        json={
            "content_base64": base64.b64encode(holdings_bytes()).decode("ascii"),
            "label": "开源证券 · 同花顺",
            "source_mtime": (NOW - timedelta(minutes=5)).isoformat(),
            "expected_positions": 2,
            "max_age_days": 7,
        },
    )
    assert response.status_code == 200, response.text
    imported = response.json()["import"]
    assert imported["source_kind"] == "tonghuashun"
    assert imported["expected_positions"] == 2


def test_delivery_note_text_xls_parses_actual_ths_column_variants(tmp_path):
    header = "\t".join(
        [
            "成交日期",
            "成交时间",
            "证券代码",
            "证券名称",
            "操作",
            "成交数量",
            "成交编号",
            "成交均价",
            "成交金额",
            "余额",
            "发生金额",
            "佣金",
            "印花税",
            "过户费",
            "合同编号",
            "交易市场",
            "其他费用",
        ]
    )
    rows = [
        "20261020\t14:00:35\t002129\tTCL中环\t证券买入\t100\t1\t8.800\t880.000\t1200.00\t-880.100\t0.100\t0.000\t0.000\t1\t深圳Ａ股\t0.000",
        "20261021\t09:31:08\t002129\tTCL中环\t证券卖出\t100\t2\t9.000\t900.000\t1200.00\t899.800\t0.100\t0.000\t0.000\t2\t深圳Ａ股\t0.000",
        "20261022\t16:00:00\t601985\tXD中国核电\t股息入账\t0\t\t0.000\t0.000\t100.00\t2.000\t0.000\t0.000\t0.000\t0\t上海Ａ股\t0.000",
        "20261022\t16:00:00\t601985\tXD中国核电\t股息红利税补缴\t0\t\t0.000\t0.000\t100.00\t-0.400\t0.000\t0.000\t0.000\t0\t上海Ａ股\t0.000",
        "20261023\t09:20:00\t001381\t新股\t申购配号\t6\t\t0.000\t0.000\t0.00\t0.000\t0.000\t0.000\t0.000\t0\t深圳Ａ股\t0.000",
    ]
    path = tmp_path / "delivery-table.xls"
    path.write_bytes(("\n".join([header, *rows])).encode("gb18030"))
    fmt, records = parse_file(path)
    assert fmt == "tonghuashun"
    assert len(records) == 4
    assert records[0].datetime == "2026-10-20 14:00:35"
    assert records[0].symbol == "002129.SZ"
    assert records[0].price == pytest.approx(8.8)
    assert records[0].fee == pytest.approx(0.1)
    assert records[2].side == "dividend"
    assert records[2].amount == pytest.approx(2.0)
    assert records[3].amount == pytest.approx(-0.4)
