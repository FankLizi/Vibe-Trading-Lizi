"""Strict reader for 同花顺 holdings exports.

The desktop client writes a GBK, tab-delimited table with an ``.xls`` suffix.
This reader treats the export as evidence only: it never contacts a broker,
never infers an account identity, and refuses rows whose exported market value
does not agree with quantity times the exported market price.
"""

from __future__ import annotations

import csv
import io
import re
from decimal import Decimal, InvalidOperation
from typing import Any

BROKER = "tonghuashun"
MAX_IMPORT_BYTES = 4 * 1024 * 1024

_REQUIRED_COLUMNS = (
    "证券代码",
    "证券名称",
    "股票余额",
    "成本价",
    "市价",
    "市值",
    "交易市场",
)
_CODE_RE = re.compile(r"^\d{6}$")
_MARKET_BY_LABEL = {
    "上海Ａ股": "SH",
    "上海A股": "SH",
    "深圳Ａ股": "SZ",
    "深圳A股": "SZ",
    "北京Ａ股": "BJ",
    "北京A股": "BJ",
    "北交所": "BJ",
}
_FUND_PREFIXES = ("15", "16", "18", "50", "51", "52", "56", "58")
_ZERO = Decimal("0")


class TonghuashunFormatError(ValueError):
    """Raised when a holdings export cannot be read without guessing."""


def decode_export(content: bytes) -> str:
    """Decode a 同花顺 text export using its documented desktop encodings.

    Args:
        content: Raw bytes from the exported file.

    Returns:
        Decoded text.

    Raises:
        TonghuashunFormatError: If no supported encoding can decode the file.
    """
    if not isinstance(content, bytes) or not content:
        raise TonghuashunFormatError("holdings export is empty")
    if len(content) > MAX_IMPORT_BYTES:
        raise TonghuashunFormatError("holdings export exceeds the 4 MiB import limit")
    for encoding in ("utf-8-sig", "utf-16", "gb18030", "gbk", "gb2312"):
        try:
            text = content.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\x00" not in text:
            return text
    raise TonghuashunFormatError("holdings export is not UTF-8, UTF-16, GBK or GB2312 text")


def parse_holdings(content: bytes) -> list[dict[str, Any]]:
    """Parse one 同花顺 ``资金股票`` export into portfolio position rows.

    Args:
        content: Raw bytes of the GBK tab-delimited ``.xls`` export.

    Returns:
        Normalized position rows with CNY quantity and exported prices.

    Raises:
        TonghuashunFormatError: If headers, rows, identifiers, quantities,
            prices or export valuations are missing or inconsistent.
    """
    text = decode_export(content)
    lines = [line.rstrip("\t\r ") for line in text.splitlines() if line.strip()]
    if not lines:
        raise TonghuashunFormatError("holdings export has no header")
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter="\t")
    rows = list(reader)
    headers = [value.strip() for value in rows[0]]
    while headers and not headers[-1]:
        headers.pop()
    missing = [name for name in _REQUIRED_COLUMNS if name not in headers]
    if missing:
        raise TonghuashunFormatError(
            "unrecognized 同花顺 holdings header; missing: " + ", ".join(missing)
        )
    index = {name: headers.index(name) for name in headers}
    positions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, row in enumerate(rows[1:], start=2):
        if not any(value.strip() for value in row):
            continue
        if len(row) < len(headers):
            row = [*row, *([""] * (len(headers) - len(row)))]
        cell = lambda name: row[index[name]].strip() if name in index else ""
        code = _normalize_code(cell("证券代码"), line_number)
        quantity = _positive_decimal(cell("股票余额"), "股票余额", line_number)
        available = _optional_decimal(cell("可用余额"), "可用余额", line_number)
        frozen = _optional_decimal(cell("冻结数量"), "冻结数量", line_number)
        if available is not None and frozen is not None and available + frozen != quantity:
            raise TonghuashunFormatError(
                f"row {line_number}: 可用余额 + 冻结数量 does not equal 股票余额"
            )
        cost = _optional_decimal(cell("成本价"), "成本价", line_number)
        price = _positive_decimal(cell("市价"), "市价", line_number)
        market_value = _positive_decimal(cell("市值"), "市值", line_number)
        expected_value = quantity * price
        tolerance = max(Decimal("0.02"), abs(market_value) / Decimal("10000"))
        if abs(expected_value - market_value) > tolerance:
            raise TonghuashunFormatError(
                f"row {line_number}: 市值 {market_value} does not match 股票余额 × 市价 "
                f"({expected_value}); refusing to guess which value is current"
            )
        suffix = _market_suffix(cell("交易市场"), code, line_number)
        symbol = f"{code}.{suffix}"
        if symbol in seen:
            raise TonghuashunFormatError(
                f"row {line_number}: duplicate securities code {symbol}"
            )
        seen.add(symbol)
        name = cell("证券名称")
        if not name or any(ord(character) < 32 for character in name):
            raise TonghuashunFormatError(
                f"row {line_number}: 证券名称 must be printable text"
            )
        holding_days = _optional_decimal(cell("持股天数"), "持股天数", line_number)
        positions.append(
            {
                "broker": BROKER,
                "source": "tonghuashun_export",
                "symbol": symbol,
                "quote_symbol": symbol,
                "name": name,
                "asset_type": _asset_type(code),
                "market": suffix,
                "currency": "CNY",
                "price_currency": "CNY",
                "quantity": float(quantity),
                "cost_price": float(cost) if cost is not None and cost > 0 else None,
                "market_price": float(price),
                "source_market_value": float(market_value),
                "holding_days": (
                    float(holding_days)
                    if holding_days is not None and holding_days >= 0
                    else None
                ),
                "priced": True,
                "as_of_source": "file_mtime",
            }
        )
    if not positions:
        raise TonghuashunFormatError("holdings export contains no non-zero positions")
    return positions


def _normalize_code(value: str, line_number: int) -> str:
    """Return a six-digit A-share code without accepting a lookalike suffix."""
    code = value.strip()
    if code.endswith(".0") and code[:-2].isdigit():
        code = code[:-2]
    if not _CODE_RE.fullmatch(code):
        raise TonghuashunFormatError(
            f"row {line_number}: 证券代码 must be a six-digit A-share code"
        )
    return code


def _market_suffix(value: str, code: str, line_number: int) -> str:
    """Map the export market label to the loader's suffix form."""
    suffix = _MARKET_BY_LABEL.get(value.strip())
    if suffix:
        return suffix
    inferred = (
        "SH"
        if code[0] in {"5", "6", "9"}
        else "SZ"
        if code[0] in {"0", "1", "2", "3"}
        else "BJ"
        if code[0] in {"4", "8"}
        else None
    )
    if inferred is None:
        raise TonghuashunFormatError(
            f"row {line_number}: unsupported 交易市场 {value!r}"
        )
    return inferred


def _asset_type(code: str) -> str:
    """Classify exchange-traded funds by their A-share code range."""
    return "etf" if code.startswith(_FUND_PREFIXES) else "stock"


def _positive_decimal(value: str, field: str, line_number: int) -> Decimal:
    parsed = _optional_decimal(value, field, line_number)
    if parsed is None or parsed <= 0:
        raise TonghuashunFormatError(
            f"row {line_number}: {field} must be a positive number"
        )
    return parsed


def _optional_decimal(value: str, field: str, line_number: int) -> Decimal | None:
    text = value.strip().replace(",", "")
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise TonghuashunFormatError(
            f"row {line_number}: {field} is not a number: {value!r}"
        ) from exc
    if not parsed.is_finite():
        raise TonghuashunFormatError(
            f"row {line_number}: {field} must be finite"
        )
    return parsed
