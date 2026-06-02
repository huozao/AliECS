from tplus_datahub.modules._pending import raise_pending


def build_price_report(*args, **kwargs):
    raise_pending("price")
