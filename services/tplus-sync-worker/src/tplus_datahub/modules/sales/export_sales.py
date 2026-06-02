from tplus_datahub.modules._pending import raise_pending


def export_sales(*args, **kwargs):
    raise_pending("sales")
