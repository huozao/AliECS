from tplus_datahub.modules._pending import raise_pending


def sync_supplier(*args, **kwargs):
    raise_pending("supplier")
