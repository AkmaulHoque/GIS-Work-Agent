# -*- coding: utf-8 -*-

def classFactory(iface):
    from .gis_work_agent import GISWorkAgentPlugin
    return GISWorkAgentPlugin(iface)
