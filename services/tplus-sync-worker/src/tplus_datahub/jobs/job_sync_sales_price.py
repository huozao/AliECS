from tplus_datahub.jobs._pending_job import run_pending_job


def main() -> int:
    return run_pending_job("sales_price")


if __name__ == "__main__":
    raise SystemExit(main())
