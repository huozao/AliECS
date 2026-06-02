from tplus_datahub.modules._pending import raise_pending


def sync_purchase_price(*args, **kwargs):
    raise_pending("purchase_price")
