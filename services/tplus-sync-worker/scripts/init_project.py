from pathlib import Path


RUNTIME_DIRS = [
    "data/raw/bom",
    "data/raw/material",
    "data/raw/product",
    "data/raw/purchase_price",
    "data/raw/sales_price",
    "data/raw/cost",
    "data/processed/bom",
    "data/processed/material",
    "data/processed/product",
    "data/processed/purchase_price",
    "data/processed/sales_price",
    "data/processed/cost",
    "data/db",
    "output/excel",
    "output/html",
    "output/logs",
]


def main() -> int:
    for dirname in RUNTIME_DIRS:
        Path(dirname).mkdir(parents=True, exist_ok=True)
        print(f"已确认目录：{dirname}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
