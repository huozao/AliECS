from tplus_datahub.modules._pending import raise_pending


def sync_product(*args, **kwargs):
    raise_pending("product")
