# -*- coding: utf-8 -*-
"""Small Qt/QGIS compatibility helpers for QGIS 3 (Qt5) and QGIS 4 (Qt6)."""

from qgis.PyQt.QtCore import Qt

try:  # Qt5
    from qgis.PyQt.QtWidgets import QAction
except ImportError:  # Qt6
    from qgis.PyQt.QtGui import QAction

from qgis.core import Qgis


def _qt_enum(legacy_name, scoped_name, member_name):
    value = getattr(Qt, legacy_name, None)
    if value is not None:
        return value
    scope = getattr(Qt, scoped_name, None)
    if scope is None:
        raise AttributeError("Qt enum not available: %s/%s.%s" % (legacy_name, scoped_name, member_name))
    return getattr(scope, member_name)


LEFT_DOCK = _qt_enum("LeftDockWidgetArea", "DockWidgetArea", "LeftDockWidgetArea")
RIGHT_DOCK = _qt_enum("RightDockWidgetArea", "DockWidgetArea", "RightDockWidgetArea")


def qgis_message_level(name):
    """Return a Qgis message level on both QGIS 3 and QGIS 4."""
    direct = getattr(Qgis, name, None)
    if direct is not None:
        return direct
    enum_cls = getattr(Qgis, "MessageLevel", None)
    if enum_cls is not None:
        return getattr(enum_cls, name)
    raise AttributeError("QGIS message level is unavailable: %s" % name)
