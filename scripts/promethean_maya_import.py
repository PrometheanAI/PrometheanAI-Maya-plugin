__author__ = 'Andrew Maximov info@prometheanai.com'
import os
import sys


def promethean_startup():
    import maya.cmds as cmds
    promethean_folders = [os.path.dirname(__file__)]
    sys.path = list(set(sys.path + promethean_folders))
    from maya.utils import executeDeferred
    # run PrometheanAI server
    executeDeferred("import maya.cmds as cmds\n"
                    "import promethean_maya_server, promethean_maya\n"
                    "promethean_maya_server.start_server()")
