from tplus_datahub.modules._pending import raise_pending


def transform_customer_rows(*args, **kwargs):
    raise_pending("customer")
