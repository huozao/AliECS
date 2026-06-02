from tplus_datahub.modules._pending import raise_pending


def build_cost_report(*args, **kwargs):
    raise_pending("cost")
