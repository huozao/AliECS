from tplus_datahub.modules._pending import raise_pending


def transform_supplier_rows(*args, **kwargs):
    raise_pending("supplier")
