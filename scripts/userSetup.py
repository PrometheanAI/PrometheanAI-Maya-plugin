# PrometheanAI Startup script for Autodesk Maya

import maya.cmds as cmds


def load_promethean_maya_plugin():
    cmds.loadPlugin('PrometheanAI', quiet=True)


cmds.evalDeferred(load_promethean_maya_plugin)
