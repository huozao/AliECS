from tplus_datahub.modules._pending import raise_pending


def sync_cost(*args, **kwargs):
    raise_pending("cost")
