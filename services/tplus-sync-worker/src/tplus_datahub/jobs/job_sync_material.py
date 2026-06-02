from tplus_datahub.jobs._pending_job import run_pending_job


def main() -> int:
    return run_pending_job("material")


if __name__ == "__main__":
    raise SystemExit(main())
