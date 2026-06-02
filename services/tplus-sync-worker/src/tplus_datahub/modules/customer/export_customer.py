from tplus_datahub.modules._pending import raise_pending


def export_customer(*args, **kwargs):
    raise_pending("customer")
