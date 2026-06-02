from tplus_datahub.modules._pending import raise_pending


def export_material(*args, **kwargs):
    raise_pending("material")
