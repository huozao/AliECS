from tplus_datahub.modules._pending import raise_pending


def sync_material(*args, **kwargs):
    raise_pending("material")
