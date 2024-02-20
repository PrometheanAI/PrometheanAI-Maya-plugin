import sys

import maya.cmds as cmds
import maya.OpenMayaUI as OpenMayaUI
import maya.api.OpenMaya as OpenMaya

try:
    from PySide2.QtWidgets import QApplication
except ImportError:
    from PySide.QtGui import QApplication


import promethean_maya_qt_server
import promethean_maya_drop
import promethean_maya_shortcut
maya_server = None

# Do not remove, necessary to indicate that this plugin uses OpenMaya2 (not joking)
def maya_useNewAPI():
    pass


class PrometheanAIPlugin(OpenMaya.MPxCommand):
    def __init__(self):
        super(PrometheanAIPlugin, self).__init__()


def initializePlugin(mobj):
    global maya_server
    plugin = OpenMaya.MFnPlugin(mobj, 'PrometheanAI', '1.0', 'Any')

    try:
        # - avoid initializing promethean server if Maya is executed in batch mode (or through command line)
        if cmds.about(batch=True):
            return
        maya_server = promethean_maya_qt_server.MayaServer()
    except Exception as exc:
        sys.stderr.write('Error while setting Promethean AI server: {}'.format(exc))
        raise

    # - setup promethean callbacks
    try:
        promethean_maya_drop.PrometheanDropCallback.instance = promethean_maya_drop.PrometheanDropCallback()
        OpenMayaUI.MExternalDropCallback.addCallback(promethean_maya_drop.PrometheanDropCallback.instance)
        sys.stdout.write("Successfully registered callback: PrometheanDropCallback\n")
    except Exception as exc:
        sys.stderr.write("Failed to register callback: PrometheanDropCallback : {}\n".format(exc))
        raise

    # - setup promethean shortcuts
    try:
        promethean_maya_shortcut.initialize_shortcuts()
        sys.stdout.write("Successfully created Promethean custom callbacks\n")
    except Exception as exc:
        sys.stderr.write("Failed to create Promethean custom hotkeys: {}\n".format(exc))
        raise

    # - we make sure that Promethean plugin is unloaded (and server is closed) when Maya app is closed
    def _uninitialize_plugin():
        try:
            is_loaded = cmds.pluginInfo('PrometheanAI.py', loaded=True, q=True)
            if is_loaded:
                cmds.unloadPlugin('PrometheanAI.py')
        except Exception:
            pass
    app = QApplication.instance()
    if app:
        app.aboutToQuit.connect(_uninitialize_plugin)


def uninitializePlugin(mobj):
    global maya_server
    plugin = OpenMaya.MFnPlugin(mobj)

    # - stop custom promethean shortcuts
    try:
        promethean_maya_shortcut.disable_shortcuts()
        sys.stdout.write("Successfully disabled Promethean custom callbacks\n")
    except Exception:
        pass

    # - remove promethean callbacks
    try:
        OpenMayaUI.MExternalDropCallback.removeCallback(promethean_maya_drop.PrometheanDropCallback.instance)
        sys.stdout.write("Successfully deregistered callback: PrometheanDropCallback\n")
    except Exception:
        sys.stderr.write("Failed to deregister callback: PrometheanDropCallback\n")
        raise

    if not cmds.about(batch=True):
        if maya_server:
            maya_server.disconnect()
        print('PrometheanAI: Server Closed')
