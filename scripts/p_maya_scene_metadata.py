__author__ = 'Andrew Maximov info@prometheanai.com'
# *********************************************************************
#  +++ IMPORTS
# *********************************************************************
import os
import json
import time
from maya import mel
import maya.cmds as cmds


# *********************************************************************
#  +++ GLOBALS
# *********************************************************************
TEMP_CACHE_FOLDER = 'C:\\PrometheanAI_Temp\\'
TEMP_THUMBNAIL_FOLDER = 'C:\\PrometheanAI_Temp\\'
KILL_TERM = '_kill_'

FLOAT_PRECISION = 3
VALID_ART_FLAGS = []
MATERIAL_TYPE = None
MATERIAL_PATH_ATTR = None

DCC_TO_PROMETHEAN_SCALE = 100


# *********************************************************************
#  +++ FUNCTIONS
# *********************************************************************
def extract_maya_scene_metadata(scene_path=None):
    """
    extract metadata from current open maya scene. currently extracted attributes are:
    bounding_box, collision, dependencies, depth, extra_flags, faces, frames, height, joints, loadedRefNodes,
    lods, materials, meshes, panFile, textures, tris, unloadedRefNodes, uvs, verts, width

    :view_fit - should the camera focus on valid geo. good for thumbnail capture
    :return a dictionary with the above keys
    """
    scene_path = scene_path or cmds.file(sceneName=1, q=1)
    # - get scene metadata
    scene_data_dict = {}
    scene_data_dict['name'] = scene_path[scene_path.rfind('/') + 1:].split('.')[0]  # file name, no extension
    scene_data_dict['path'] = scene_path  # use full path
    relative_file_path = scene_path  # TODO: change this if necessary
    scene_data_dict['type'] = 'mesh'
    scene_data_dict['date'] = os.path.getmtime(scene_path)  # last modified. could be by perforce
    # - get lod data
    lod_0_meshes, lod_group_dict = get_lod_data()
    scene_data_dict['lods'] = lod_group_dict
    scene_data_dict['lod_num'] = len(scene_data_dict['lods'])
    # - extra flags - will return every option we found these set to
    scene_data_dict['extra_flags'] = {x: [] for x in VALID_ART_FLAGS}
    for FLAG in VALID_ART_FLAGS:  # get all the unique values for every transform if flag exists
        scene_data_dict['extra_flags'][FLAG] += list(set(
            [cmds.getAttr(x + '.' + FLAG) for x in cmds.ls(type='transform', l=1) if
             cmds.attributeQuery(FLAG, node=x, exists=True)]))
    # - get valid geometry
    all_meshes = get_valid_render_meshes()
    all_lod_meshes = get_valid_lod_group_meshes()
    non_lod_meshes = set(all_meshes) - set(all_lod_meshes)  # - set(flat_refnode_children)  # keep refnode chidren for accurate polycount
    valid_meshes = list(non_lod_meshes) + lod_0_meshes
    # - get number of meshes
    scene_data_dict['meshes'] = len(valid_meshes)
    # - get faces, vertexes, uvs
    poly_data = cmds.polyEvaluate(valid_meshes)
    scene_data_dict['face_count'] = scene_data_dict['verts'] = scene_data_dict['uvs'] = 0.0
    if str(poly_data) != 'Nothing counted : no polygonal object is selected.':  # when nothing to polyEvaluate maya returns this
        scene_data_dict['face_count'] = poly_data['triangle']
        scene_data_dict['verts'] = poly_data['vertex']
        scene_data_dict['uvs'] = poly_data['uvcoord']
    # TODO:
    scene_data_dict['vertex_color_channels'] = 1
    scene_data_dict['uv_sets'] = 1
    # - get dimensions
    bbox = cmds.polyEvaluate(valid_meshes, b=1)
    if str(bbox) == 'Nothing counted : no polygonal object is selected.':
        bbox = None
    scene_data_dict['bounding_box'] = get_dimensions(bbox)
    scene_data_dict['bounding_box'] = convert_to_dcc_scale(scene_data_dict['bounding_box'])  # convert to cm
    scene_data_dict['pivot_offset'] = get_pivot_offset(bbox) if bbox else [0.0, 0.0, 0.0]
    # - get materials
    scene_data_dict['material_paths'] = []
    scene_data_dict['material_count'] = 0
    if MATERIAL_PATH_ATTR and MATERIAL_TYPE:
        shading_engines = list(set(cmds.listConnections(valid_meshes, type='shadingEngine') or []))
        material_nodes = list(set(cmds.listConnections(shading_engines, type=MATERIAL_TYPE) or []))
        material_paths = sorted(list(set([cmds.getAttr(x + '.' + MATERIAL_PATH_ATTR).lower() for x in material_nodes])))
        scene_data_dict['material_paths'] = material_paths
        scene_data_dict['material_count'] = len(scene_data_dict['materials'])
    # - joints and animation
    scene_data_dict['joints'] = len(cmds.ls(type='joint', l=1))
    # - capture_image
    file_name, file_extension = os.path.splitext(relative_file_path)
    thumbnail_path = os.path.join(TEMP_THUMBNAIL_FOLDER, file_name.replace('/', '--').replace(':', '-')) + '.png'
    scene_data_dict['thumbnail'] = thumbnail_path
    # sorted for easier asset similarity identification
    if 'dependencies' in scene_data_dict:
        scene_data_dict['dependencies'] = sorted(list(set(scene_data_dict['dependencies'])))
    valid_meshes = get_valid_render_meshes()
    if valid_meshes:
        capture_image(valid_meshes, scene_data_dict['thumbnail'])
    return scene_data_dict


def write_current_scene_metadata_to_file(scene_path, learning_cache_path=None):
    # - make sure target paths exist
    if not os.path.exists(TEMP_CACHE_FOLDER):
        os.makedirs(TEMP_CACHE_FOLDER)
    if not os.path.exists(TEMP_THUMBNAIL_FOLDER):
        os.makedirs(TEMP_THUMBNAIL_FOLDER)
    # - check if cache data exists
    learning_cache_path = learning_cache_path or os.path.join(TEMP_CACHE_FOLDER, r"learning_cache.json")
    try:
        existing_data = load_json_file(learning_cache_path)
    except:  # sometimes we read the file faster then it's finished being written to on the windows side
        time.sleep(3)
        existing_data = load_json_file(learning_cache_path)
    # - extract data from current scene write it to cache data file
    data = extract_maya_scene_metadata(scene_path)  # do this before the file is open in case there is crash
    if data:
        existing_data[data['path']] = data  # add to existing file
        with open(learning_cache_path, 'w') as f:
            json.dump(existing_data, f)


def ___IMAGE_CAPTURE___():  # utility function for visual code separation
    pass


def capture_image(valid_meshes, image_path):
    if not os.path.exists(TEMP_THUMBNAIL_FOLDER):
        os.makedirs(TEMP_THUMBNAIL_FOLDER)
    # - keep original selection
    current_selection = cmds.ls(sl=1, l=1)   # keep original selection
    # - only display valid_meshes
    cmds.hide(cmds.ls(type='mesh', l=1))  # all flag just hides top hierarchy level which is not good for us
    cmds.showHidden(valid_meshes)
    cmds.refresh(force=1)  # force update viewport
    # - get distance to object to zoom in closer
    bbox = cmds.polyEvaluate(valid_meshes, b=1)
    camera_angle = get_camera_angle(bbox)
    # - orient camera
    camera = 'persp'  # assuming that default perspective camera exists
    cmds.lookThru(camera)
    cmds.rotate(camera_angle[0], camera_angle[1], camera_angle[2], camera, absolute=1)  # front and to the right camera orientation
    cmds.select(valid_meshes)
    cmds.viewFit()
    diameter = max(get_dimensions(bbox)) * 1.5  # magic pull back number
    cmds.dolly(camera, abs=True, d=diameter)
    # capture and restore selection
    cmds.select(clear=1)
    hide_hud()
    capture_viewport(image_path)
    cmds.select(current_selection)


def hide_hud():
    cmds.grid(toggle=0)
    # mel.eval("refNodeToggle -da 0;;")  # hide refnode UI stuff - TODO: NEED SMS SPECIFIC COMMAND
    mel.eval("hideShow -kinematics -hide;")  # hide all joint stuff
    mel.eval("setViewAxisVisibility(0);")
    mel.eval("setCameraNamesVisibility(0);")
    mel.eval("setPolyCountVisibility(0);")
    mel.eval("setObjectDetailsVisibility(0);")
    mel.eval("setFrameRateVisibility(0);")
    mel.eval("setSelectDetailsVisibility(0);")
    mel.eval("setCapsLockVisibility(0);")
    mel.eval("setSceneTimecodeVisibility(0);")
    mel.eval("viewManip - v(0);")


def capture_viewport(path, size=2048):
    """ capture image from current maya scene """
    # TODO: set background black? currently transparent
    # set hud
    cmds.setAttr("defaultRenderGlobals.imageFormat", 32)  # set output to png
    cmds.playblast(frame=[0], format='image', completeFilename=path, viewer=False, offScreen=True,
                   height=size, width=size, percent=100)


def ___GET_DATA___():  # utility function for visual code separation
    pass


def get_lod_data():
    """ extract lod data only from lod groups that are not inside expanded refnodes"""
    all_lod_groups = get_valid_lod_groups() or []
    fit_lod_groups = set(all_lod_groups)
    lod_group_dict = {}
    lod_0_meshes = []  # these will be useful later in determining valid meshes to get stats from
    for lod_group in fit_lod_groups:
        if KILL_TERM in lod_group:
            continue
        lod_group_dict[lod_group] = {'distances': [x for x in cmds.getAttr(lod_group + '.threshold')[0] if x]}
        children = cmds.listRelatives(lod_group, children=1, f=1)
        lod_group_dict[lod_group]['hide'] = not bool(cmds.listRelatives(children[-1], allDescendents=1, f=1))  # check if last element has no children
        lod_0_meshes += cmds.listRelatives(children[0], allDescendents=1, type='mesh', f=1) or []  # if not children
    return lod_0_meshes, lod_group_dict


def get_dimensions(bbox):
    if not bbox:
        return [1.0, 1.0, 1.0]
    width = bbox[0][1] - bbox[0][0]
    height = bbox[1][1] - bbox[1][0]
    depth = bbox[2][1] - bbox[2][0]
    return [round(x, FLOAT_PRECISION) for x in [width, height, depth]]


def get_pivot_offset(bbox):
    x = bbox[0][0] + (bbox[0][1] - bbox[0][0]) / 2.0
    y = bbox[1][0]
    z = bbox[2][0] + (bbox[2][1] - bbox[2][0]) / 2.0
    return [-round(o, FLOAT_PRECISION) for o in [x, y, z]]


def get_camera_angle(bbox):
    """ hanging items like chandeliers usually have pivot on top and at origin
        if center below zero we look at it from above """
    above = (-30, 45, 0)
    below = (40, 45, 0)
    return below if (bbox[1][0] + (bbox[1][1] - bbox[1][0]) / 2.0) < 0.0 else above


def convert_to_dcc_scale(in_list):
    return [round(x * DCC_TO_PROMETHEAN_SCALE, 2) for x in in_list]  # convert to cm


def ___GET_NODES___():  # utility function for visual code separation
    pass


def get_valid_lod_groups():
    all_lod_groups = cmds.ls(type='lodGroup', l=1) or []
    return all_lod_groups


def get_valid_lod_group_meshes():
    return cmds.listRelatives(get_valid_lod_groups(), allDescendents=1, type='mesh', f=1) or []


def get_valid_render_transforms():
    render_meshes = get_valid_render_meshes()
    render_transforms = cmds.listRelatives(render_meshes, parent=1, type='transform', f=1)
    return render_transforms or []


def get_valid_render_meshes():
    all_meshes = set(cmds.ls(type='mesh', l=1))
    valid_meshes = all_meshes
    return list(valid_meshes)


def get_kill_meshes():
    kill_nodes = [x for x in cmds.ls(l=1) if KILL_TERM in x]
    kill_meshes = list(set(cmds.listRelatives(kill_nodes, allDescendents=1, type='mesh', f=1) or []))
    return kill_meshes


def get_kill_transforms():
    kill_meshes = get_kill_meshes()
    kill_transforms = cmds.listRelatives(kill_meshes, parent=1, type='transform', f=1)
    return kill_transforms or []


def get_transforms_by_material_path(material_path):
    out_transforms = []
    for shading_engine in cmds.ls(type='shadingEngine', l=1):
        material_nodes = cmds.listConnections(shading_engine, type=MATERIAL_TYPE)
        if not material_nodes:
            continue
        material_paths = [cmds.getAttr(x + '.' + MATERIAL_PATH_ATTR) for x in material_nodes]
        if any(x.startswith(material_path) for x in material_paths):
            out_transforms += cmds.listConnections(shading_engine, type='mesh') or []
    return list(set(out_transforms))


def ___MISC___():
    pass


def load_json_file(file_path):
    # not using this function from promethean generic because this file has to be read by the maya interpreter
    # that will not find all the dependencies
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            return json.load(f) or {}
    return {}
