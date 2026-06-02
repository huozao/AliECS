from tplus_datahub.modules._pending import raise_pending


def transform_product_rows(*args, **kwargs):
    raise_pending("product")
