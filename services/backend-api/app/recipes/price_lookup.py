"""最新采购/销售价格查找：读取 tplus-sync-worker 导出的价格 Excel，
为成本核算提供「某存货编码的最新含税单价 + 单据日期」。

数据源与 BOM 输入同目录（/app/tplus-output/excel，共享卷），由 release 同步产出
purchase_price_*.xlsx / sales_price_*.xlsx。找不到文件/列时优雅降级为空。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pandas as pd

PURCHASE_PREFIX = "purchase_price_"
SALES_PREFIX = "sales_price_"

_CODE_COL = "存货编码"
_PRICE_COL = "含税单价"  # 价格口径：含税单价（与销售价格一致，便于直接比毛利）
_DATE_COL = "单据日期"


def _resolve_dir(export_dir: str | Path | None) -> Path:
    if export_dir is not None:
        return Path(export_dir)
    return Path(os.getenv("TPLUS_EXPORT_DIR", "/app/tplus-output/excel"))


def _latest_file(directory: Path, prefix: str) -> Path | None:
    if not directory.exists():
        return None
    files = [p for p in directory.glob(f"{prefix}*.xlsx") if p.is_file() and not p.name.startswith("~$")]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


# 按文件内容签名缓存解析结果：sales/purchase 价格 xlsx 解析约 0.6~3s，避免每次 /cost 重复解析。
_PRICE_CACHE: dict[str, dict[str, dict[str, object]]] = {}


def _file_content_signature(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_latest_prices(path: Path) -> dict[str, dict[str, object]]:
    signature = _file_content_signature(path)
    cached = _PRICE_CACHE.get(signature)
    if cached is not None:
        return cached
    result = _read_latest_prices_uncached(path)
    if len(_PRICE_CACHE) >= 6:  # 采购+销售各一份，留些余量后整体清空限制内存
        _PRICE_CACHE.clear()
    _PRICE_CACHE[signature] = result
    return result


def _read_latest_prices_uncached(path: Path) -> dict[str, dict[str, object]]:
    df = pd.read_excel(path, dtype={_CODE_COL: str})
    if _CODE_COL not in df.columns or _PRICE_COL not in df.columns:
        return {}
    df = df.copy()
    df["_code"] = df[_CODE_COL].astype(str).str.strip()
    df["_price"] = pd.to_numeric(df[_PRICE_COL], errors="coerce")
    df["_dt"] = pd.to_datetime(df.get(_DATE_COL), errors="coerce")
    df = df[(df["_code"] != "") & df["_price"].notna() & (df["_price"] > 0)]
    if df.empty:
        return {}
    # 按单据日期升序（无日期排前），同编码取最后一条即「最新且含税单价>0」。
    df = df.sort_values("_dt", na_position="first", kind="stable")
    latest = df.drop_duplicates("_code", keep="last")
    result: dict[str, dict[str, object]] = {}
    for _, row in latest.iterrows():
        dt = row["_dt"]
        result[str(row["_code"])] = {
            "price": float(row["_price"]),
            "date": "" if pd.isna(dt) else dt.strftime("%Y-%m-%d"),
        }
    return result


def latest_purchase_prices(export_dir: str | Path | None = None) -> dict[str, dict[str, object]]:
    path = _latest_file(_resolve_dir(export_dir), PURCHASE_PREFIX)
    return _read_latest_prices(path) if path else {}


def latest_sales_prices(export_dir: str | Path | None = None) -> dict[str, dict[str, object]]:
    path = _latest_file(_resolve_dir(export_dir), SALES_PREFIX)
    return _read_latest_prices(path) if path else {}
