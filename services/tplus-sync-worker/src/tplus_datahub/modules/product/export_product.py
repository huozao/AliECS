from tplus_datahub.modules._pending import raise_pending


def export_product(*args, **kwargs):
    raise_pending("product")
