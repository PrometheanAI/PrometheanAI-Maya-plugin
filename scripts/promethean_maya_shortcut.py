from functools import partial

import maya.cmds as cmds
import maya.OpenMayaUI as OpenMayaUI

try:
    from PySide2.QtCore import Qt, QEvent, QCoreApplication
    from PySide2.QtWidgets import QWidget, QShortcut
    from PySide2.QtGui import QKeySequence, QKeyEvent
    from shiboken2 import wrapInstance
except ImportError:
    from PySide.QtCore import Qt, QEvent, QCoreApplication
    from PySide.QtGui import QWidget, QKeySequence, QShortcut, QKeyEvent
    from shiboken import wrapInstance


PROMETHEAN_SHORTCUTS_ENABLED = False
PROMETHEAN_SHORTCUTS_INITIALIZED = False


def _get_main_maya_window():
    maya_main_window_ptr = OpenMayaUI.MQtUtil.mainWindow()
    maya_main_window = wrapInstance(int(maya_main_window_ptr), QWidget)
    return maya_main_window


def initialize_shortcuts():
    global PROMETHEAN_SHORTCUTS_ENABLED
    global PROMETHEAN_SHORTCUTS_INITIALIZED

    PROMETHEAN_SHORTCUTS_ENABLED = True

    # - avoid to initialize shortcuts multiple times
    if PROMETHEAN_SHORTCUTS_INITIALIZED:
        return

    PROMETHEAN_SHORTCUTS_ENABLED = True
    PROMETHEAN_SHORTCUTS_INITIALIZED = True
    # - example hotkey
    # shortcut = QShortcut(QKeySequence(Qt.Key_Delete), _get_main_maya_window())
    # shortcut.setContext(Qt.ApplicationShortcut)
    # shortcut.activated.connect(partial(_on_delete_command, shortcut))


def disable_shortcuts():
    global PROMETHEAN_SHORTCUTS_ENABLED
    PROMETHEAN_SHORTCUTS_ENABLED = False
