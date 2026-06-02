from tplus_datahub.modules._pending import raise_pending


def export_sales_price(*args, **kwargs):
    raise_pending("sales_price")
