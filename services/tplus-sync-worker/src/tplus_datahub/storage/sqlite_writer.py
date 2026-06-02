from __future__ import annotations

from tplus_datahub.core.exceptions import EndpointNotConfirmedError


def write_sqlite(*args, **kwargs):
    raise EndpointNotConfirmedError("SQLite 沉淀暂未实现，第一阶段先输出原始 JSON 和 Excel")
