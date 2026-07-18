# Cura MCP plugin — registration entry point.
# Copyright (c) 2026 gaetan.deturche. Released under the LGPLv3 or higher
# (same terms as the Uranium/Cura APIs it builds on).

from . import CuraMCP


def getMetaData():
    return {}


def register(app):
    return {"extension": CuraMCP.CuraMCP()}
