import os
import traceback

import maya.OpenMayaUI as OpenMayaUI

import promethean_maya


class PrometheanDropCallback(OpenMayaUI.MExternalDropCallback):
    instance = None

    def __init__(self):
        super(PrometheanDropCallback, self).__init__()

    def externalDropCallback(self, do_drop, control_name, data):
        # Extract asset paths
        if data.hasUrls():
            file_paths = [url.partition('file:///')[-1] for url in data.urls()]
        else:
            file_paths = data.text().split('\n')
        file_paths = [file_path for file_path in file_paths if file_path and os.path.isfile(file_path) and os.path.splitext(file_path)[-1] in ['.ma', '.mb']]

        # Default Maya behaviour for everything except Promethean assets and ma/mb files
        if not data.hasFormat('promethean/asset_hashes') and not file_paths:
            return OpenMayaUI.MExternalDropCallback.kMayaDefault

        # Accept if any of the files exist
        if do_drop == 0:
            if file_paths:
                return OpenMayaUI.MExternalDropCallback.kNoMayaDefaultAndAccept
            else:
                return OpenMayaUI.MExternalDropCallback.kNoMayaDefaultAndNoAccept
        else:
            # Actual drop
            try:
                promethean_maya.drop_asset(file_paths, data.hasFormat('promethean/asset_hashes'))
            except Exception as exc:
                print('PrometheanAI: Error on the drop')
                traceback.print_exc()
            finally:
                return OpenMayaUI.MExternalDropCallback.kNoMayaDefaultAndAccept

