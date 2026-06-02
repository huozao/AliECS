from tplus_datahub.modules._pending import raise_pending


def transform_purchase_price_rows(*args, **kwargs):
    raise_pending("purchase_price")
