from tplus_datahub.modules._pending import raise_pending


def sync_sales(*args, **kwargs):
    raise_pending("sales")
