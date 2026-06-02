from tplus_datahub.modules._pending import raise_pending


def export_supplier(*args, **kwargs):
    raise_pending("supplier")
