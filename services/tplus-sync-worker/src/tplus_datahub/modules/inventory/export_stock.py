from tplus_datahub.modules._pending import raise_pending


def export_stock(*args, **kwargs):
    raise_pending("inventory")
