__author__ = 'Andrew Maximov andrew@prometheanai.com'

import os
import re
import json
import logging
from functools import partial
import math

try:
    from PySide2 import QtCore
    from PySide2.QtWidgets import QApplication, QWidget
    from PySide2.QtGui import QCursor
    from shiboken2 import wrapInstance
except ImportError:
    from PySide import QtCore
    from PySide.QtGui import QApplication, QWidget, QCursor
    from shiboken import wrapInstance

import maya.cmds as cmds
import maya.mel as mel
from maya.utils import executeInMainThreadWithResult as mayaExec
import maya.api.OpenMaya as om
import maya.OpenMaya as om1
import maya.OpenMayaUI as omui1
from maya.app.mayabullet import RigidBody

import p_maya_scene_metadata

# - setup logger
logger = logging.getLogger('p_maya_plugin')
logger.setLevel(logging.INFO)
fh = logging.FileHandler(os.path.join(os.path.dirname(__file__), 'p_maya_plugin.log'))
fh.setFormatter(logging.Formatter(
    '[%(levelname)1.1s  %(asctime)s | %(name)s | %(module)s:%(funcName)s:%(lineno)d] > %(message)s'))
fh.setLevel(logging.INFO)
logger.addHandler(fh)

_simulated_nodes = list()
_snap_timer = QtCore.QTimer()
_snap_timer.setInterval(100)
_snap_active = False

uuid_regex = re.compile('[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.IGNORECASE)

REFERENCE_ATTRIBUTE_NAME = 'promethean_reference'


# Decorator to execute the function in one chunk
def one_undo_chunk(func):
    def inner(*args, **kwargs):
        cmds.undoInfo(openChunk=True)
        try:
            return func(*args, **kwargs)
        except:
            raise
        finally:
            cmds.undoInfo(closeChunk=True)
    return inner

# Units conversion
units_multiplier = 1.0


def set_current_units_multiplier():
    global units_multiplier
    units_multiplier = float(cmds.convertUnit(1.0, toUnit='cm').replace('cm', ''))
    # print('Promethean AI: units multiplier has changed to %s' % units_multiplier)

unit_changed_job = cmds.scriptJob( e= ["linearUnitChanged", set_current_units_multiplier])
set_current_units_multiplier()

old_nodes_names_dict = {}

def cache_playback_options():
    global playback_options
    playback_options = [cmds.playbackOptions(q=True, minTime=True),
                        cmds.playbackOptions(q=True, maxTime=True),
                        cmds.playbackOptions(q=True, loop=True)]

def restore_playback_options():
    global playback_options
    cmds.playbackOptions(minTime=playback_options[0])
    cmds.playbackOptions(maxTime=playback_options[1])
    cmds.playbackOptions(loop=playback_options[2])

playback_options = []
cache_playback_options()


# =====================================================================
# +++ MAYA TCP MESSAGE RECEIVER
# =====================================================================
@one_undo_chunk
def command_switch(meta_command_str):
    global units_multiplier
    global old_nodes_names_dict
    """ central hub for tcp commands coming into maya.
    :param connection: connection socket from the server to send the data back across
    :param meta_command_str: incoming str from Promethean standalone. could be multiple commands
    """
    logger.debug('incoming command: ', meta_command_str)
    # this file must imported since it's executed so we can reference internal functions directly

    for command_str in meta_command_str.split('\n'):
        if not command_str:  # if str is blank
            continue
        logger.debug('command string: ', command_str)
        command_list = command_str.split(' ')
        command = command_list[0]

        msg = 'DefaultValue'  # return message
        if command == 'get_scene_name':
            msg = str(cmds.file(q=1, sceneName=1, shortName=1)) or 'None'

        elif command == 'get_selection':
            selection = nodes_to_promethean_names(cmds.ls(sl=True, l=True))
            msg = json.dumps(selection) if selection else 'None'

        elif command == 'save_current_scene':
            cmds.file(save=True)

        elif command == 'get_visible_static_mesh_actors':
            on_screen = getObjectsInView()
            msg = str(nodes_to_promethean_names(on_screen))

        elif command == 'get_selected_and_visible_static_mesh_actors':
            selection_list = cmds.ls(sl=True, tr=True, l=True)   # [u'|bedroom', ...]
            on_screen_list = getObjectsInView()
            scene_name = str(cmds.file(q=1, sceneName=1, shortName=1))

            selected_paths_dict = {}
            for i, obj_name in enumerate(selection_list):
                selected_paths_dict.setdefault(get_reference_path(obj_name), []).append(i)
            rendered_paths_dict = {}
            for i, obj_name in enumerate(on_screen_list):
                rendered_paths_dict.setdefault(get_reference_path(obj_name), []).append(i)

            msg = json.dumps({'selected_names': nodes_to_promethean_names(selection_list),
                              'rendered_names': nodes_to_promethean_names(on_screen_list),
                              'selected_paths': selected_paths_dict,
                              'rendered_paths': rendered_paths_dict, 'scene_name': scene_name})

        elif command == 'get_location_data' and len(command_list) > 1:
            p_names = command_list[1].split(',')
            data_dict = {p_name: [x * units_multiplier for x in cmds.xform(node, t=True, q=True, ws=True)]
                         for node, p_name in zip(get_nodes_from_promethean_names(p_names), p_names) if node}
            msg = json.dumps(data_dict)

        elif command == 'get_pivot_data' and len(command_list) > 1:
            p_names = command_list[1].split(',')
            data_dict = dict()
            for p_name in p_names:
                node = get_node_from_promethean_name(p_name)
                if node:
                    size, rotation, pivot, pivot_offset = getTransform(node)  # warning - can't do semantic objects
                    data_dict[p_name] = pivot
            msg = json.dumps(data_dict)

        elif command == 'get_transform_data' and len(command_list) > 1:
            p_names = command_list[1].split(',')
            data_dict = dict()
            for p_name in p_names:
                node = get_node_from_promethean_name(p_name)
                if node:
                    data = getRawObjectData(node, is_group=isGroup(node))
                    data_dict[p_name] = data['transform'] + data['size'] + data['pivot_offset'] + [data['parent_name']]
            msg = json.dumps(data_dict)

        elif command == 'add_objects' and len(command_list) > 1:
            """ add a group of objects and send back a dictionary of their final dcc names
            takes a json string as input that is a dictionary with old dcc name ask key and this dict as value 
            (asset_path, name, location, rotation, scale) """
            obj_dict = json.loads(command_str.replace('add_objects ', ''))  # json str has spaces so doing this
            return_dict = {}
            for old_dcc_name in obj_dict:
                new_object = add_object(obj_dict[old_dcc_name])
                if new_object:
                    old_nodes_names_dict[old_dcc_name] = new_object
                    return_dict[old_dcc_name] = node_to_promethean_name(new_object)
            msg = json.dumps(return_dict)
        elif command == 'add_mesh_on_selection' and len(command_list) > 1:
            meshes_paths = command_list[1].split(',')
            add_meshes_on_selection(meshes_paths)
        elif command == 'add_objects_from_polygons' and len(command_list) > 1:
            obj_list = json.loads(command_str.replace('add_objects_from_polygons ', ''))  # str has spaces so doing this
            add_objects_from_polygons(obj_list)

        elif command == 'add_objects_from_triangles' and len(command_list) > 1:
            obj_list = json.loads(command_str.replace('add_objects_from_triangles', ''))  # str has spaces so doing this
            return_dict = add_objects_from_triangles(obj_list)
            msg = json.dumps(return_dict)  # return names of newly created objects

        elif command == 'parent' and len(command_list) > 1:
            p_names = command_list[1].split(',')
            nodes = get_nodes_from_promethean_names(p_names)
            parent = nodes.pop(0)  # first one is the parent
            children = nodes
            if parent:
                if cmds.objExists(parent):
                    def _get_valid_children():
                        valid_children = list()
                        valid_chldren_exists = [x for x in children if x and cmds.objExists(x)]
                        for valid_child in valid_chldren_exists:
                            child_parent = cmds.listRelatives(valid_child, parent=True, f=True)
                            if child_parent:
                                child_parent = child_parent[0]
                                if not cmds.objExists(child_parent):
                                    continue
                                parent_uuid = cmds.ls(child_parent, uuid=True)[0]
                                if parent_uuid == parent:
                                    continue
                            valid_children.append(valid_child)
                        return valid_children
                    valid_children = _get_valid_children()
                    if valid_children:
                        cmds.parent(valid_children, parent)

        elif command == 'unparent' and len(command_list) > 1:
            p_names = command_list[1].split(',')
            nodes = get_nodes_from_promethean_names(p_names)
            objs = [x for x in nodes if x and cmds.listRelatives(x, parent=True)]
            if objs:
                cmds.parent(objs, world=1)

        elif command == 'remove' and len(command_list) > 1:
            p_names = command_list[1].split(',')
            nodes = get_nodes_from_promethean_names(p_names)
            # Remove nodes from the old names dict
            old_nodes_names_dict = {key: value for key, value in old_nodes_names_dict.items() if value not in nodes}
            remove(nodes)

        elif command == 'get_parents' and len(command_list) > 1:
            def _parent_list(objects_names):
                return [cmds.listRelatives(x, parent=1, f=1) if x else None for x in objects_names]
            p_names = command_list[1].split(',')
            nodes = get_nodes_from_promethean_names(p_names)
            parent_list = _parent_list(nodes)  # need to make sure we return the same amount of elements
            parent_list = [x[0] if x else 'No_Parent' for x in parent_list]  # get parent returns a single item list or None
            parent_list = nodes_to_promethean_names(parent_list)
            msg = ','.join(parent_list)

        elif command == 'isolate_selection':
            isolate_selection()

        elif command == 'select':
            p_names = command_list[1].split(',')
            nodes = get_nodes_from_promethean_names(p_names)
            cmds.select([x for x in nodes if x], replace=True)

        elif command == 'set_visible':
            p_names = command_list[1].split(',')
            nodes = get_nodes_from_promethean_names(p_names)
            cmds.showHidden([x for x in nodes if x])

        elif command == 'set_hidden':
            p_names = command_list[1].split(',')
            nodes = get_nodes_from_promethean_names(p_names)
            cmds.hide([x for x in nodes if x])

        elif command == 'focus':
            p_names = command_list[1].split(',')
            nodes = get_nodes_from_promethean_names(p_names)
            cmds.viewFit([x for x in nodes if x])

        elif command == 'open_file' and len(command_list) > 1:  # open and learn a file
            file_path = command_str.replace('open_file ', '')
            open_file(file_path)
            msg = json.dumps(True)  # return a message once it's done

        elif command == 'learn_file' and len(command_list) > 1:  # open and learn a file
            learn_dict = json.loads(command_str.replace('learn_file ', ''))
            learn_file(learn_dict.get('file_path', ''), learn_dict.get('learn_file_path', ''),
                     extra_tags=learn_dict.get('tags', list()),  project=learn_dict.get('project', ''),
                     from_selection=False)
            msg = json.dumps(True)  # return a message once it's done

        elif command == 'get_vertex_data_from_scene_objects' and len(command_list) > 1:
            p_names = command_list[1].split(',')
            nodes = get_nodes_from_promethean_names(p_names)
            out_dict = dict()
            for node, p_name in zip(nodes, p_names):
                vert_list = get_triangle_positions(node, direction_mask=False)  # direction mask to adjust for thick wall objects
                vert_list = [{i: vert for i, vert in enumerate(vert_list)}]  # TODO this is to keep parity with UE4 integration. should be fixed eventually
                out_dict[p_name] = vert_list
            msg = json.dumps(out_dict)

        elif command == 'get_vertex_data_from_scene_object' and len(command_list) > 1:
            p_name = command_list[1]
            node = get_node_from_promethean_name(p_name)
            if node:
                vert_list = get_triangle_positions(node, direction_mask=False)  # direction mask to adjust for thick wall objects
                vert_list = [{i: vert for i, vert in enumerate(vert_list)}]  # TODO this is to keep parity with UE4 integration. should be fixed eventually
                vert_dict = {'vertex_positions': vert_list}
                msg = json.dumps(vert_dict)

        elif command == 'setup_learn_scene' and len(command_list) > 1:
            # need import into a blank scene because existing scene have playblast gamma settings that we can't fix :(
            path = command_str.replace('setup_learn_scene ', '')
            cmds.file(f=True, new=True)  # open blank scene
            cmds.modelEditor('modelPanel4', e=1, displayTextures=1)  # set shaded
            cmds.file(path, i=1)  # import

        elif command == 'learn_asset_file' and len(command_list) > 1:
            path = command_str.replace('learn_asset_file ', '')  # in case there are spaces in the path
            p_maya_scene_metadata.write_current_scene_metadata_to_file(path)  # need to execute in main thread

        elif command == 'report_done':
            msg = json.dumps('Done')  # return a message once it's done

        elif command == 'screenshot' and len(command_list) > 1:
            path = command_str.replace('screenshot ', '')  # in case there are spaces in the path
            cmds.setAttr("defaultRenderGlobals.imageFormat", 32)  # set format to PNG # TODO watch out for enum changes
            cmds.playblast(frame=[0], format="image", viewer=0, offScreen=1, completeFilename=path, percent=100)  # offScreen in thread breaks viewport

        elif command == 'remove_descendents' and len(command_list) > 1:
            p_names = command_list[1].split(',')
            nodes = get_nodes_from_promethean_names(p_names)
            remove_descendants([x for x in nodes if x])

        elif command == 'rename' and len(command_list) > 2:
            source_p_name = command_list[1]
            target_name = command_list[2]
            source_node = get_node_from_promethean_name(source_p_name)
            if source_node:
                cmds.rename(source_node, target_name)

        elif command in ['translate', 'scale', 'rotate', 'translate_relative', 'scale_relative',
                         'rotate_relative'] and len(command_list) > 1:
            value = [float(x) / units_multiplier if command in ['translate', 'translate_relative'] else float(x) for x in command_list[1].split(',')]
            p_names = command_list[2].split(',')
            nodes = get_nodes_from_promethean_names(p_names)
            func = None
            relative = command.endswith('_relative')
            if 'translate' in command:
                func = cmds.move
            elif 'rotate' in command:
                func = cmds.rotate
            elif 'scale' in command:
                func = cmds.scale
            if func:
                nodes = [x for x in nodes if x]
                if relative:
                    func(value[0], value[1], value[2], nodes, relative=True)
                else:
                    func(value[0], value[1], value[2], nodes, absolute=True)

        elif command == 'translate_and_snap' and len(command_list) > 4:
            location = [float(x) / units_multiplier for x in command_list[1].split(',')]
            raytrace_distance = float(command_list[2]) / units_multiplier
            max_normal_deviation = float(command_list[3])
            nodes = get_nodes_from_promethean_names(command_list[4].split(','))
            ignore_nodes = get_nodes_from_promethean_names(command_list[5].split(','))
            translate_and_raytrace_by_name(nodes, location, raytrace_distance, max_normal_deviation, ignore_nodes)

        elif command == 'translate_and_raytrace' and len(command_list) > 3:
            location = [float(x) / units_multiplier for x in command_list[1].split(',')]
            raytrace_distance = float(command_list[2]) / units_multiplier
            max_normal_deviation = 0
            nodes = get_nodes_from_promethean_names(command_list[3].split(','))
            ignore_nodes = get_nodes_from_promethean_names(command_list[4].split(','))
            translate_and_raytrace_by_name(nodes, location, raytrace_distance, max_normal_deviation, ignore_nodes)

        elif command == 'set_mesh' and len(command_list) > 2:
            mesh_path = command_list[1]
            p_names = command_list[2].split(',')
            nodes = get_nodes_from_promethean_names(p_names)
            set_mesh(mesh_path, nodes)

        elif command == 'set_mesh_on_selection' and len(command_list) > 1:
            mesh_path = command_list[1]
            selected_nodes = cmds.ls(sl=True, tr=1)
            set_mesh(mesh_path, selected_nodes)

        elif command == 'load_assets' or command == 'drop_asset':
            # The drop operation is managed by custom Promethean Drop Callback
            print('PrometheanAI: Ignore "' + command + '" command because drag and drop is handled by a custom callback')
            return

        elif command in ['raytrace', 'raytrace_bidirectional'] and command_list:
            distance = float(command_list[1]) / units_multiplier
            p_name = command_list[2]
            node = get_node_from_promethean_name(p_name)
            if node:
                hit_object, hit_position, hit_normal = find_close_intersection_point(node, [0, -1, 0], distance=distance)
                msg = {p_name: [x * units_multiplier for x in hit_position] if hit_position else [0.0, 0.0, 0.0]}

        elif command in ('get_simulation_on_actors_by_name', 'get_transform_data_from_simulating_objects'):
            msg = 'None'

        elif command == 'enable_simulation_on_objects' and command_list:

            # make sure bullet plugin is loaded
            bullet_loaded = cmds.pluginInfo('bullet.mll', loaded=True, q=True)
            if not bullet_loaded:
                cmds.loadPlugin('bullet.mll')

            p_names = command_list[1].split(',')
            nodes = get_nodes_from_promethean_names(p_names)
            for node in nodes:
                set_as_dynamic_object(node)
            static_nodes = get_potential_static_nodes(nodes)
            for static_node in static_nodes:
                set_as_static_object(static_node)
            _simulated_nodes = nodes + static_nodes

        elif command == 'start_simulation':
            cache_playback_options()
            cmds.playbackOptions(minTime=1, maxTime=1000, loop='once')
            mel.eval('playButtonStart;')
            cmds.play(forward=True)

        elif command == 'cancel_simulation':
            cmds.play(state=False)
            mel.eval('playButtonStart;')
            mel.eval('bullet_DeleteEntireSystem;')
            restore_playback_options()

        elif command == 'end_simulation':
            mel.eval('bullet_DeleteEntireSystem;')
            cmds.play(state=False)
            mel.eval('playButtonStart;')
            restore_playback_options()

        elif command == 'toggle_surface_snapping':
            global _snap_active
            _snap_active = not _snap_active

        elif command == 'create_assets_from_selection' and len(command_list) > 1:
            msg = create_asset_from_selection(root_path=command_str[len('create_assets_from_selection '):])

        else:  # pass through - standalone sends actual DCC code
            mayaExec(command_str)

        if msg != 'DefaultValue':  # sometimes functions return a None and we need to communicate it back
            msg = msg or 'None'  # sockets won't send empty messages so sending 'None' as a string
            if type(msg) != str:
                msg = json.dumps(msg)
            return msg


# =====================================================================
# +++ MAYA EDIT FUNCTIONS
# =====================================================================
def remove_unknown_nodes():
    is_binary = cmds.file(q=1, type=1) == ['mayaBinary']
    if is_binary:
        unknown_nodes = cmds.ls(exactType='unknown')
        if unknown_nodes:
            cmds.delete(unknown_nodes)


def get_world_position(transform_):
    # regardless of frozen transforms
    return cmds.xform(transform_, pivots=1, q=1, ws=1)[:3]


def move_to_origin(transform_):
    # regardless of frozen transforms
    offset = get_world_position(transform_)
    offset = [x * y for x, y in zip(offset, [-1] * len(offset))]
    cmds.move(offset[0], offset[1], offset[2], transform_, relative=1)


def create_asset_from_selection(root_path):
    # TODO: only top parents
    selection = cmds.ls(sl=1, tr=1, long=1)
    new_paths = {}
    errors = []
    if selection:
        remove_unknown_nodes()
        top_transforms = []
        # Make sure we only have transforms that have any mesh in descendents
        selection_with_meshes_under = set([x for x in selection if cmds.listRelatives(x, allDescendents=1, type="mesh")])
        # Identify top transforms to be used for export
        for node in selection_with_meshes_under:
            parents = cmds.listRelatives(node, allParents=1, fullPath=1)
            if not parents or not set(parents) & selection_with_meshes_under:
                top_transforms.append(node)
        if top_transforms:
            # Check if some meshes are already assets
            existing_assets_transforms = {transform: cmds.getAttr('%s.%s' % (transform, REFERENCE_ATTRIBUTE_NAME))
                                          for transform in top_transforms if
                                          cmds.attributeQuery(REFERENCE_ATTRIBUTE_NAME, node=transform, exists=True)}

            for top_transform in top_transforms:
                export_file_path = existing_assets_transforms.get(top_transform, None)
                # Collapse geometry if needed
                # new_transform = merge_geometry(top_transform)
                new_transform = top_transform  # keeping like this for a moment in case we want to merge again
                # Cache world position
                original_transform = cmds.xform(new_transform, matrix=1, q=1, ws=1)
                # Reset position
                move_to_origin(new_transform)
                # Freeze all transforms as we assume that's the final geometry
                # cmds.makeIdentity(new_transform, t=True, r=True, s=True, apply=True, n=0, pn=True)
                cmds.select(new_transform)
                # Find safe path if it's a new asset
                if not export_file_path or not os.path.isfile(export_file_path):
                    name = cmds.ls(new_transform, shortNames=1)[0]
                    # Replace all the special characters
                    name = re.sub('[^0-9a-zA-Z-_]+', '_', name)
                    # Make sure we don't override existing file, cause the names may clash
                    maya_scene_name = os.path.normpath(str(cmds.file(q=1, sceneName=1, shortName=0)))
                    import hashlib
                    hash_directory = os.path.dirname(maya_scene_name)
                    path_hash = hashlib.md5(hash_directory.encode()).hexdigest()  # unique hash folder per maya file
                    export_file_path = os.path.join(root_path, path_hash, "%s.%s" % (name, 'ma'))
                # - create folders
                export_dir = os.path.dirname(export_file_path)
                if not os.path.exists(export_dir):
                    os.makedirs(export_dir)
                # - select all the children for export
                cmds.select(cmds.listRelatives(top_transform, allDescendents=1, f=1) + [top_transform])
                # Actual export
                export_file_path = cmds.file(export_file_path, force=True, type='mayaAscii', exportSelected=1,
                                             preserveReferences=1)
                # - check if export was successful
                if not os.path.isfile(export_file_path):
                    cmds.confirmDialog(title='Warning', message='Couldn\'t export file: ' + str(export_file_path),
                                       button=['OK'])
                    continue
                # cmds.delete(new_transform)
                cmds.xform(new_transform, matrix=original_transform, ws=1)  # - reset position
                # don't reference new assets on top because we don't remove the original any more at this point
                # imported_meshes = reference_asset(export_file_path, world_pos=world_pos)
                # if imported_meshes:
                #    new_paths[imported_meshes[0]] = export_file_path
                new_paths[new_transform] = export_file_path
        else:
            cmds.confirmDialog(title='Warning', message='No meshes were found among the selected objects',
                               button=['OK'])
    if new_paths:
        cmds.select(list(new_paths.keys()))
    return list(new_paths.values())


def add_object(obj_dict):
    global units_multiplier
    out_name = None

    rotation = obj_dict['rotation']
    location = [x / units_multiplier for x in obj_dict['location']]

    # First we check if there are any conditions based on raytracing
    raytrace_distance =  obj_dict.get('raytrace_distance', None)

    # Store if we need to rotate after the raytracing
    y_rotation_needed = False
    if raytrace_distance:
        raytrace_distance = raytrace_distance / units_multiplier
        start_point = [location[0], location[1] + raytrace_distance * 0.5, location[2]]
        hit_object, hit_position, hit_normal = raytrace(start_point, [0, -1, 0], raytrace_distance, ignore_dcc_names=[out_name])
        if hit_object:
            location = hit_position
            raytrace_alignment =  obj_dict.get('raytrace_alignment', None)
            raytrace_alignment_mask =  obj_dict.get('raytrace_alignment_mask', None)
            if raytrace_alignment or raytrace_alignment_mask:
                up_dot_product = dot([0, 1, 0], hit_normal)
                # if alignment mask was not passed, skip object creation
                if up_dot_product < raytrace_alignment_mask:
                    return None
                # If we pass the threshold, align the object to the normal
                if up_dot_product > raytrace_alignment:
                    y_rotation_needed = True
                    rotation = cmds.angleBetween(euler=True, v1=(0.0, 1.0, 0.0), v2=hit_normal)

    if obj_dict['group']:
        out_name = cmds.group(empty=True, name=obj_dict['name'])
    elif obj_dict['asset_path']:
        added_objects = reference_asset(obj_dict['asset_path'])
        if added_objects:
            out_name = added_objects[0]
    if not out_name:
        # TODO: make sure unit scale is respected
        out_name = cmds.polyCube(name=obj_dict['name'], width=100, height=100, depth=100)[0]

    cmds.xform(out_name,
             translation=location, rotation=rotation, scale=obj_dict['scale'], ws=1)
    # Use the rotation around Y to apply to already rotated to match the normal mesh
    if y_rotation_needed:
        cmds.rotate(obj_dict['rotation'][1], out_name, rotateY=True, ws=False)

    parent_name = obj_dict.get('parent_dcc_name', '')
    if parent_name:
        dcc_parent_name = get_node_from_promethean_name(parent_name)
        if dcc_parent_name:
            out_name = cmds.parent(out_name, dcc_parent_name)[0]
        else:
            logger.debug('Parent to attach was not found: %s' % parent_name)

    return out_name

def set_mesh(mesh_path, nodes_names):
    global old_nodes_names_dict
    global units_multiplier
    for node_name in nodes_names:
        path = get_reference_path(node_name)
        if path == mesh_path:
            continue
        # Save parent and children of the original node
        parent = cmds.listRelatives(node_name, p=True, f=True)
        parent = cmds.ls(parent, uuid=True) if parent else None
        children = cmds.listRelatives(node_name, children=True, type='transform', f=True)
        children = cmds.ls(children, uuid=True) if children else None
        new_node = reference_asset(mesh_path, world_pos=[x * units_multiplier for x in cmds.xform(node_name, q=True, t=True, ws=True)], world_rot=cmds.xform(node_name, q=True, ro=True, ws=1))
        if new_node:
            new_node = new_node[0]
            # Return the hierarchy back
            if parent:
                parent = cmds.ls(parent, l=True)
                new_node = cmds.parent(new_node, parent)[0]
            if children:
                children = cmds.ls(children, l=True)
                cmds.parent(children, new_node, absolute=1)
            # We save the old p_name to the dictionary to be able to retrieve the new node if some promethean command sends the old name
            old_nodes_names_dict[node_to_promethean_name(node_name)] = node_to_promethean_name(new_node)

        remove([node_name])


def add_meshes_on_selection(meshes_paths):
    selection = cmds.ls(sl=1, tr=1)
    new_meshes = []
    for mesh_path in meshes_paths:
        new_meshes.extend(reference_asset(mesh_path))
    if len(selection):
        cmds.xform(new_meshes, translation=cmds.xform(selection[0], t=True, q=True, ws=True), ws=True)
    cmds.select(new_meshes)


def add_objects_from_polygons(geometry_list):
    """ each item in the list is a dictionary that stores a polygon based object
        TODO: a single object is currently one polygon. Need multi-polygon objects
     {'name': 'FixedFurniture CoatCloset',
      'points': [(0.0, 0.0, 0.0),
                 (103.69, 0.0, 0.0),
                 (103.69, 0.0, 60.0),
                 (0.0, 0.0, 60.0)],
      'transform': [0.0, 0.0, -1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 937.9074, 0.0, 192.8589, 1.0]}] # """
    for geometry_dict in geometry_list:
        cmds.polyCreateFacet(p=geometry_dict['points'])  # polygons create triangulation issues?
        # cmds.curve(p=geometry_dict['points'], d=0)
        if 'transform' in geometry_dict:  # only furniture has a transform so far
            cmds.xform(matrix=geometry_dict['transform'])
        cmds.rename(geometry_dict['name'])


def add_objects_from_triangles(geometry_dicts):
    """ input is a dictionary with a unique dcc_name for a key and a dictionary that stores a triangle-based object value
    { dcc_name:
     {'name': 'FixedFurniture CoatCloset',
      'tri_ids': [(0, 1, 2),(0, 2, 3), (0, 1, 4) ... ],
      'verts': [(0.0, 0.0, 0.0), (103.69, 0.0, 0.0), (103.69, 0.0, 60.0), (), (), ... ], - unique vertexes
      'normals': [(0.0, 1.0, 0.0), (0.0, 1.0, 0.0), (0.0, 1.0, 0.0), (), (), ... ], - normal per tri, matching order
      'transform': [0.0, 0.0, -1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 937.9074, 0.0, 192.8589, 1.0]},
      dcc_name: {}
    } """
    out_names = {}
    for dcc_name in geometry_dicts:
        geometry_dict = geometry_dicts[dcc_name]
        temp_new_objects = []
        unique_verts = geometry_dict['verts']
        logging.info('Constructing %s' % geometry_dict['name'])
        for i, tri_vert_id in enumerate(geometry_dict['tri_ids']):
            tri = [unique_verts[tri_vert_id[0]], unique_verts[tri_vert_id[1]], unique_verts[tri_vert_id[2]]]
            tri = check_winding_order(tri, geometry_dict['normals'][i])
            temp_new_objects.append(mayaExec("cmds.polyCreateFacet(p=%s)" % tri))  # create geo in main thread
            mayaExec("cmds.polyNormalPerVertex(xyz=%s)" % geometry_dict['normals'][i])  # set normal for new object
        mayaExec("cmds.polyUnite(*%s, n='%s')" % (temp_new_objects, geometry_dict['name']))
        mayaExec("cmds.polyMergeVertex(d=0.1)")  # merge verts with threshold distance at 0.1
        mayaExec("cmds.delete(ch=1)")
        mayaExec("cmds.xform(centerPivots=1)")        #
        if 'transform' in geometry_dict:  # only furniture has a transform so far
            mayaExec("cmds.xform(matrix=%s)" % geometry_dict['transform'])
        out_names[dcc_name] = get_node_from_promethean_name(cmds.ls(sl=1, l=1)[0])  # ls returns long name
    return out_names


def check_winding_order(tri, normal):
    """ making sure triangles face the intended way """
    ab = [a-b for a, b in zip(tri[0], tri[1])]
    ac = [a-b for a, b in zip(tri[0], tri[2])]
    cross_product = cross(ab, ac)
    if dot(normal, cross_product) < 0:
        tri.reverse()
    return tri


def cross(a, b):
    """ cross product, maya doesn't have numpy """
    c = [a[1]*b[2] - a[2]*b[1],
         a[2]*b[0] - a[0]*b[2],
         a[0]*b[1] - a[1]*b[0]]
    return c


def dot(a, b):
    """ dimension agnostic dot product """
    return sum([x * y for x, y in zip(a, b)])


def remove_descendants(parents_names):
    for parent_name in parents_names:
        children = cmds.listRelatives(parent_name, children=True, f=True)
        if children:
            cmds.delete(children)
        else:
            logging.info('No objects found: %s' % parent_name)


def open_file(path, force=False):
    path = path.replace('\\', '/')  # important!
    if not path or not os.path.isfile(path):
        print('File: "%s" doesn\'t exist' % path)
        return False

    if cmds.file(q=True, anyModified=True) and not force:
        result = cmds.confirmDialog(title='Save File?', message='File contains unsaved changes.',
                                    button=['Save', 'Don\'t Save', 'Cancel'], defaultButton='Save',
                                    cancelButton='Cancel', dismissString='Cancel')
        if result == 'Save':
            cmds.file(s=True)
            cmds.file(path, o=True)
        if result == 'Don\'t Save':
            cmds.file(path, o=True, force=True)
        if result == 'Cancel':
            return False
    else:
        cmds.file(path, o=True, force=force)
    mel.eval('addRecentFile("' + path + '", "mayaAscii")')
    return True


# =====================================================================
# +++ MAYA LEARN FUNCTIONS
# =====================================================================
def learn_file(file_path, learn_file_path, extra_tags=[], project=None, from_selection=False):
    cmds.file(file_path, open=1, force=1)  # will wait to open
    learn(learn_file_path, extra_tags=extra_tags, project=project, from_selection=from_selection)


def learn(file_path, extra_tags=[], project=None, from_selection=False):
    cmds.delete(all=1, constructionHistory=1)  # cleanup maya scene
    raw_data = getAllObjectsRawData(selection=from_selection)
    scene_id = cmds.file(q=1, sceneName=1, shortName=1) + '/'
    learningCacheDataToFile(file_path, raw_data, scene_id, extra_tags, project)


def learningCacheDataToFile(file_path, raw_data, scene_id, extra_tags=[], project=None):
    import json
    learning_dict = {'raw_data': raw_data, 'scene_id': scene_id}
    if len(extra_tags) > 0:
        learning_dict['extra_tags'] = extra_tags
    if project is not None:
        learning_dict['project'] = project
    with open(file_path, 'w') as f:
        f.write(json.dumps(learning_dict))


def getRawObjectData(maya_transform_node, is_group=False):
    global units_multiplier
    # - get transform
    if not is_group:
        size, rotation, pivot, pivot_offset = getTransform(maya_transform_node)  # accurate min xz bbox
    else:
        # TODO: get the pivot in local space relative to the bottom of the bounding box
        pivot = cmds.xform(maya_transform_node, scalePivot=1, q=1, ws=1)  # actual maya pivot position
        pivot_offset = [0, 0, 0]
        rotation = cmds.xform(maya_transform_node, rotation=1, q=1)
        size = cmds.xform(maya_transform_node, scale=1, q=1, r=1)
    translation = cmds.xform(maya_transform_node, scalePivot=1, q=1, ws=1)  # actual maya pivot position
    translation = [x * units_multiplier for x in translation]
    scale = cmds.xform(maya_transform_node, scale=1, q=1, r=1)
    transform = translation + rotation + scale  # WARNING! Instead of using a transform matrix we simplify to t,t,t,r,r,r,s,s,s
    size = [max(1.0, x) for x in size]  # make sure there is no zero size, at least 1 unit
    parent_dcc_name = cmds.listRelatives(maya_transform_node, parent=1, f=1)
    parent_dcc_name = node_to_promethean_name(parent_dcc_name[0]) if parent_dcc_name else ''
    # TODO: missing art_asset_path !
    return {'raw_name': node_to_promethean_name(maya_transform_node), 'parent_name': parent_dcc_name, 'is_group': is_group,
            'size': size, 'rotation': rotation, 'pivot': pivot, 'pivot_offset': pivot_offset, 'transform': transform}


def getAllObjectsRawData(selection=False):
    transforms = cmds.ls(selection=True, l=1, type='transform') if selection else cmds.ls(type='transform', l=1)
    mesh_transforms = [x for x in transforms if cmds.listRelatives(x, allDescendents=1, f=1, type='mesh')]
    # - get all mesh objects
    object_data_array = []
    for mesh_transform in mesh_transforms:
        if '_kill_' not in mesh_transform:
            is_group = not bool(cmds.listRelatives(mesh_transform, children=1, f=1, type='mesh'))
            obj_data = getRawObjectData(mesh_transform, is_group=is_group)
            object_data_array.append(obj_data)
    return object_data_array


def isGroup(transform):
    """ very loose definition - transform with no mesh directly parented """
    return cmds.objectType(transform) == 'transform' and not cmds.listRelatives(transform, children=1, type='mesh')


def getPivot(bbox):
    # pivot is at the center of bounding box on XZ and is min on Y
    return [(bbox[0][1] - bbox[0][0])/2.0 + bbox[0][0], bbox[1][0], (bbox[2][1] - bbox[2][0])/2.0 + bbox[2][0]]


def getSize(bbox):
    return [bbox[0][1] - bbox[0][0], bbox[1][1] - bbox[1][0], bbox[2][1] - bbox[2][0]]


def getSmallestBoudningBox(obj_dcc_names, fast=False):
    """ get bbox accounting for some maya quirks """
    filtered_dcc_names = [obj for obj in obj_dcc_names if cmds.objExists(obj)]
    missing_dcc_names = [x for x in obj_dcc_names if x not in filtered_dcc_names]
    if missing_dcc_names:
        logging.warning('These meshes no longer exist. What happened? ', missing_dcc_names)
    obj_dcc_names = filtered_dcc_names
    # world space
    if fast:
        # returns tuple ((xmin,xmax), (ymin,ymax), (zmin,zmax))
        bbox = cmds.polyEvaluate(obj_dcc_names, b=1)  # sometime buggy for no apparent reason
    else:
        # return list: xmin, ymin, zmin, xmax, ymax, zmax
        xmin, ymin, zmin, xmax, ymax, zmax = cmds.exactWorldBoundingBox(obj_dcc_names)
        # we convert it to have same format as polyEvaluate command
        bbox = ((xmin, xmax), (ymin, ymax), (zmin, zmax))

        # current_sel = cmds.ls(sl=1)
        # face_num_dict = {}
        # for obj in obj_dcc_names:
        #         face_num_dict[obj] = cmds.polyEvaluate(obj, face=1)
        # cmds.select(clear=1)
        # for key in face_num_dict:
        #     cmds.select('%s.f[0:%s]' % (key, face_num_dict[key]), add=1)
        # bbox = cmds.polyEvaluate(boundingBoxComponent=1)
        # cmds.select(clear=1)
        # cmds.select(current_sel)
    return bbox


def get_ls_bottom_center_and_bbox(transform):
    selection_list = om1.MSelectionList()
    selection_list.add(transform)
    # Get the current selection.
    # The first object in the selection is used, and is assumed
    # to be a transform node with a single shape directly below it.
    selected_path = om1.MDagPath()
    selection_list.getDagPath(0, selected_path)

    # Get the shape directly below the selected transform.
    selected_path.extendToShape()

    fn_mesh = om1.MFnMesh(selected_path)
    bounds = fn_mesh.boundingBox()
    center = bounds.center()
    bbox_min = bounds.min()
    return [-center.x, -bbox_min.y, -center.z], [bounds.width(), bounds.height(), bounds.depth()]


def getTransform(obj_name):
    """ get size, rotation, pivot """
    if not cmds.objExists(obj_name):  # check if valid input otherwise the following will crash
        return [0, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0]

    global units_multiplier
    # - get transform data
    rotation = cmds.xform(obj_name, rotation=1, q=1, ws=True)
    om_rotation = om.MEulerRotation(*[math.radians(x) for x in rotation])
    # - get actual world space pivot position
    ws_pivot = cmds.xform(obj_name, sp=1, q=1, ws=1)
    # - get local space pivot position. we use scale pivot as it comes without applying the scale
    ls_pivot = cmds.xform(obj_name, sp=1, q=1, ws=0)
    scale = cmds.xform(obj_name, scale=1, q=1, ws=True)
    # TODO: check if we need to us units_multiplier
    ls_pivot_offset, ls_bbox_size = get_ls_bottom_center_and_bbox(obj_name)

    # - that's for the tricky Maya situations like reset pivots, frozen transforms, etc.
    # ls_pivot_offset will give the vector from bottom center to bounding box center,
    # while ls_pivot - vector from bounding box center to object pivot, so the sum is just a vector from bottom center to object pivot
    real_ls_pivot_offset = [a + b for a, b in zip(ls_pivot_offset, ls_pivot)]
    # - get the actual world space bottom center position by rotating pivot offset, scaling it and subtracting from ws pivot
    ls_pivot_offset_rotated = om.MVector(*real_ls_pivot_offset).rotateBy(om_rotation)
    ls_pivot_offset_rotated = [ls_pivot_offset_rotated.x, ls_pivot_offset_rotated.y, ls_pivot_offset_rotated.z]
    ws_bottom_center = [a - b * s for a, b, s in zip(ws_pivot, ls_pivot_offset_rotated, scale)]

    size = [x * scale for x, scale in zip(ls_bbox_size, scale)]

    return size, rotation, ws_bottom_center, real_ls_pivot_offset


def minXZBBox(obj, step=2.5, max_steps=72, debug=False, fast=False, dont_revert=False):
    """ this function is used for learning data with no rotation label to figure out one of the 2 possible orientations
        based on minimizing bounding box size using a hacky version of gradient descent """
    rotation = cmds.xform(obj, rotation=1, q=1)
    # stored rotation can freeze transforms
    cmds.xform(obj, centerPivots=1)
    prev_bbox = getSmallestBoudningBox([obj])
    pivot = getPivot(prev_bbox)
    prev_bbox = getSize(prev_bbox)  # print(prev_bbox[0], prev_bbox[2])
    # get gradient
    cmds.rotate(0, step, 0, obj, relative=1)
    cmds.makeIdentity(obj, apply=1, r=1)  # otherwise bbox misfires
    bbox = getSmallestBoudningBox([obj])
    bbox = getSize(bbox)
    if bbox[0] * bbox[2] > prev_bbox[0] * prev_bbox[2]:
        step = -step  # print('reversing')
        cmds.rotate(0, step, 0, obj, relative=1)
        cmds.makeIdentity(obj, apply=1, r=1)  # otherwise bbox misfires
        i = 0
    else:
        i = 1
    # main loop
    final_angle = 0
    wrong_steps = 0
    max_wrong_steps = 3  # steps to allow to go uphill (might be because of float imprecision)
    while i < max_steps:
        cmds.rotate(0, step, 0, obj, relative=1)
        cmds.makeIdentity(obj, apply=1, r=1)  # otherwise bbox misfires
        if debug: cmds.refresh()
        bbox = getSmallestBoudningBox([obj], fast=fast)
        bbox = getSize(bbox)
        if debug:
            logger.debug('angle: ', i*step, 'bbox: ', bbox)
        i += 1
        if bbox[0] * bbox[2] < prev_bbox[0] * prev_bbox[2]:
            prev_bbox = bbox
            final_angle = step*i
        else:
            wrong_steps += 1
            if wrong_steps > max_wrong_steps:  # strange hack for now
                # step = -step/2.0  # TODO: Try adaptive step size to get better precision in less steps
                break
    if debug:
        logger.debug('Iterations: ', i)
    if not dont_revert:
        cmds.rotate(0, -step*i, 0, obj, relative=1)
    cmds.makeIdentity(obj, apply=1, r=1)  # otherwise bbox misfires
    # add back previous rotation
    cmds.xform(obj, rotation=[-x for x in rotation])
    cmds.makeIdentity(obj, apply=1, r=1)
    cmds.xform(obj, rotation=rotation)
    return prev_bbox, -final_angle, pivot


def get_triangle_positions(dcc_name, direction_mask=False):
    """ take a dcc_name of an object and return a list of vertex position of every triangle.
        every 3 consecutive positions define a triangle
        if direction mask is true - filter out face that face toward the center of the room.
        a workaround for all shells with thickness - trying to pick the interior perimeter """
    cmds.undoInfo(openChunk=True)  # open undo chunk
    bbox_center = om.MVector([x + (y - x) / 2 for x, y in cmds.polyEvaluate(dcc_name, b=1)])
    dir_mask_threshold = 0.0  # dot product value
    face_verts, debug_faces, debug_positions = [], [], []
    cmds.polyTriangulate(dcc_name)  # will select faces
    cmds.select(clear=1)  # if anything is selected will throw off logic. undo should bring back original selection
    face_num = cmds.polyEvaluate(dcc_name, face=1)
    for f_num in range(face_num):
        verts = cmds.ls(cmds.polyListComponentConversion('%s.f[%s]' % (dcc_name, f_num), fromFace=True, toVertex=True), flatten=True)
        vert_ids = element_list_to_int(verts)  # convert str list to numbers
        current_face_verts = []
        for vert_id in vert_ids:
            pos = cmds.xform('%s.vtx[%s]' % (dcc_name, vert_id), q=1, translation=1, ws=1, absolute=1)
            current_face_verts.append(pos)  # print(pos)
        if direction_mask:
            face_normal_str = cmds.polyInfo('%s.f[%s]' % (dcc_name, f_num), faceNormals=True)
            face_normal_str = face_normal_str[0].split(':')[1][1:-1]  # [u'FACE_NORMAL 1: 0.0 0.0 1.0\n']
            normal = om.MVector([float(x) for x in face_normal_str.split(' ')]).normal()
            face_center = om.MVector([sum([x[i] for x in current_face_verts])/3.0 for i in range(3)])
            debug_positions.append([face_center.x, face_center.y, face_center.z])
            dir_from_center = om.MVector(face_center - bbox_center).normal()
            if dir_from_center * normal > dir_mask_threshold:  # dot product
                debug_faces.append('%s.f[%s]' % (dcc_name, f_num))
                continue  # don't add to global face_verts list if not facing
        face_verts += current_face_verts
    cmds.undoInfo(closeChunk=True)  # close undo chunk and undo
    cmds.undo()
    # cmds.select(debug_faces, add=True)  # debug. disable undo above to see
    return face_verts


def element_list_to_int(item_list):
    """ ['pCylinder1.vtx[38]', 'pCylinder1.vtx[39]', 'pCylinder1.vtx[40]'] -> [38, 39, 40] """
    out_values = []
    for str_item in item_list:
        out_values.append(int(str_item[str_item.rfind('[')+1:str_item.rfind(']')]))
    return out_values


# =====================================================================
# +++ SCENE MANAGEMENT FUNCTIONS
# =====================================================================

def remove(dcc_names):

    def _remove(dcc_name):
        if not dcc_name or not cmds.objExists(dcc_name):
            return
        children = cmds.listRelatives(dcc_name, children=True, type='transform', allDescendents=True, fullPath=True) or list()
        for child in children:
            _remove(child)
        if is_reference(dcc_name):
            ref_node = cmds.referenceQuery(dcc_name, referenceNode=True)
            if ref_node:
                cmds.file(referenceNode=ref_node, removeReference=True)
        else:
            cmds.delete(dcc_name)

    for dcc_name in dcc_names:
        _remove(dcc_name)


# =====================================================================
# +++ MAYA REFERENCES FUNCTIONS
# =====================================================================

def is_reference(node):
    if not cmds.objExists(node):
        return False

    return cmds.referenceQuery(node, isNodeReferenced=True)

# =====================================================================
# +++ MISC FUNCTIONS
# =====================================================================

# def getObjectsInView():
#     orig_sel = cmds.ls(sl=1, l=1)
#     view = omui1.M3dView.active3dView()
#     om1.MGlobal.selectFromScreen(0, 0, view.portWidth(), view.portHeight(), om1.MGlobal.kReplaceList)
#     objs_in_view = cmds.ls(sl=1, l=1)
#     cmds.select(orig_sel)
#     return objs_in_view


def getObjectsInView():
    view = omui1.M3dView.active3dView()
    width = view.portWidth()
    height = view.portHeight()
    mdag_path = om1.MDagPath()
    view.getCamera(mdag_path)
    draw_traversal = omui1.MDrawTraversal()
    draw_traversal.setFrustum(mdag_path, width, height)
    draw_traversal.traverse()
    objs_in_view = list()
    for i in range(draw_traversal.numberOfItems()):
        shape_dag_path = om1.MDagPath()
        draw_traversal.itemPath(i, shape_dag_path)
        transform_dag_path = om1.MDagPath()
        om1.MDagPath.getAPathTo(shape_dag_path.transform(), transform_dag_path)
        obj = transform_dag_path.fullPathName()
        if cmds.objExists(obj):
            objs_in_view.append(obj)
    return objs_in_view


def get_geometry_in_view():
    geo_in_view = list()
    objs_in_view = getObjectsInView() or list()
    for obj in objs_in_view:
        if cmds.nodeType(obj) == 'transform':
            shapes = cmds.listRelatives(obj, shapes=True, fullPath=True)
        else:
            shapes = [obj]
        if shapes:
            for shape in shapes:
                if cmds.nodeType(shape) == 'mesh':
                    geo_in_view.append(obj)
    return geo_in_view


def isolate_selection():
    current_panel = cmds.paneLayout('viewPanes', q=True, pane1=True)
    cmds.isolateSelect('%s', state=1) % current_panel
    current_selection = cmds.ls(sl=1, long=1)
    previous_obj_sel_set = cmds.isolateSelect('%s', viewObjects=1, q=1) % current_panel
    if previous_obj_sel_set:  # in case empty set name
        cmds.select('%s') % previous_obj_sel_set
        cmds.isolateSelect('%s', removeSelected=1) % current_panel
    cmds.select('%s') % current_selection
    cmds.isolateSelect('%s', addSelected=1) % current_panel
    cmds.viewFit()


def pack_dcc_name(dcc_name):
    # - to avoid problems with long paths names we need to use UUIDs in Maya, for that reason we use the following
    # - dcc name format in Maya: UUID#dcc_name

    return '{}#{}'.format(cmds.ls(dcc_name, uuid=True)[0].lower(), dcc_name)


def unpack_dcc_name(dcc_name):
    name_split = dcc_name.split('#')[-1]
    return name_split.upper()   # force UUID to be upper case


# =====================================================================
# +++ BATCH EDIT LEARNING FILES FUNCTIONS
# =====================================================================
def replaceNameInFile(file_path, old, new):
    cmds.file(file_path, open=1, force=1)
    fixes = []
    for transform in cmds.ls(type='transform'):
        if old in transform:
            fixes.append('fixing %s in %s' % (transform, file_path))
            cmds.rename(transform, transform.replace(old, new))
    cmds.file(save=1)
    return fixes


# =====================================================================
# +++ MAYA ASSET FUNCTIONS
# =====================================================================

def nodes_to_promethean_names(nodes):
    # Promethean name format: object_name#uuid
    return [node_to_promethean_name(node) for node in nodes]


def node_to_promethean_name(node_name):
    # name = cmds.ls(node_name, shortNames=True)
    name = node_name.rpartition("|")[-1]
    uuid = cmds.ls(node_name, uuid=True)
    if name and uuid:
        return '%s#%s' % (name, uuid[0].lower())
    else:
        return ''


def get_nodes_from_promethean_names(names):
    return [get_node_from_promethean_name(name) for name in names]


def get_node_from_promethean_name(p_name):
    # Promethean name format: object_name#uuid
    # where ID is the Maya UUID

    # First try to retrieve name from some old name in case Promethean doesn't know the new object name
    global old_nodes_names_dict
    renamed_node = old_nodes_names_dict.get(p_name, None)
    if renamed_node:
        if cmds.ls(renamed_node):
            # print('New node was found for the old name %s: %s' % (name, node_to_promethean_name(renamed_node)))
            return renamed_node
        else:
            old_nodes_names_dict.pop(p_name)

    result = uuid_regex.search(p_name)
    if result:
        uuid = result.group(0)
        node_names = cmds.ls(uuid, uuid=True, long=True)
        if node_names:
            return node_names[0]
    else:
        # try to just match it by name and not id in case debug code for example
        node_names = cmds.ls(p_name, long=True)
        if len(node_names) == 1:
            return node_names[0]
        # print('No node found: %s\n - uuid was not specified' % p_name)
        pass
    # print('No node found: %s' % p_name)
    return None


def reference_asset(asset_path, world_pos=None, world_rot=None, is_promethean_asset=True):
    global units_multiplier

    if not asset_path:
        return list()
    # If external file, we just group imported transforms together
    if not is_promethean_asset:
        imported_objects = cmds.file(asset_path, i=True, f=True, returnNewNodes=True) or list()
        new_transforms = cmds.ls(imported_objects, transforms=1)
        if len(new_transforms) > 1:
            group = cmds.group(new_transforms, name=os.path.splitext(os.path.basename(asset_path))[0])
            new_transforms = [group]
        return new_transforms[0]
    existing_transforms = []
    if not os.path.isfile(asset_path):
        # If we pass the object name and uuid, we just try find the that object
        existing_transforms = [get_node_from_promethean_name(asset_path)]
        if not existing_transforms:
            return list()
    else:
        # Trying to find existing objects in the scene by path
        # TODO: try to retrieve the path from shapes too in case of instancing
        existing_transforms = [x for x in cmds.ls('*.%s' % REFERENCE_ATTRIBUTE_NAME, o=True, tr=True)
                               if cmds.getAttr('%s.%s' % (x, REFERENCE_ATTRIBUTE_NAME)) == asset_path]
    if existing_transforms:
        # Check if children exist and unparent them
        existing_transform_children = cmds.listRelatives(existing_transforms[0], children=True, type='transform', f=True)
        if existing_transform_children:
            existing_transform_children = cmds.parent(existing_transform_children, w=1, absolute=1)
            # existing_transform_children = cmds.ls(existing_transform_children, uuid=True)
        # Duplicate the object, parent to world and reset the transformations
        new_transforms = cmds.duplicate(existing_transforms[0], instanceLeaf=True)
        if cmds.listRelatives(new_transforms, p=True):
            new_transforms = cmds.parent(new_transforms, w=True, absolute=1)
        cmds.xform(new_transforms, t=(0, 0, 0), ro=(0, 0, 0), s=(1, 1, 1))
        # Reparent the children back
        if existing_transform_children:
            # existing_transform_children = cmds.ls(existing_transform_children, l=True)
            cmds.parent(existing_transform_children, existing_transforms[0], absolute=True)
    else:
        imported_objects = cmds.file(asset_path, i=True, f=True, returnNewNodes=True) or list()
        new_transforms = cmds.ls(imported_objects, transforms=1)
        # Combine meshes on import if more than one transform identified
        if len(new_transforms) > 1:
            group = cmds.group(new_transforms, name=os.path.splitext(os.path.basename(asset_path))[0])
            merged_geometry = merge_geometry(group)
            new_transforms = [merged_geometry]
        # Add an attribute to store the reference path to transfroms and shapes
        transforms_and_meshes = cmds.ls(new_transforms, shapes=1, transforms=1)
        for obj in transforms_and_meshes:
            if not cmds.attributeQuery(REFERENCE_ATTRIBUTE_NAME, node=obj, exists=True):
                cmds.addAttr(obj, ln=REFERENCE_ATTRIBUTE_NAME, dt='string')
            cmds.setAttr('{}.{}'.format(obj, REFERENCE_ATTRIBUTE_NAME), asset_path, type='string')

        # Freeze transforms of the imported assets to set their transformations correcty after
        cmds.makeIdentity(new_transforms, t=True, r=True, s=True, apply=True, n=0, pn=True)
    if world_pos is not None:
        world_pos = [x / units_multiplier for x in world_pos]
        for transform in new_transforms:
            if world_rot:
                cmds.xform(transform, translation=world_pos, rotation=world_rot, worldSpace=True)
            else:
                cmds.xform(transform, translation=world_pos, worldSpace=True)
    # TODO: return only the top transforms
    return new_transforms


def get_reference_path(dcc_name):
    if not dcc_name or not cmds.objExists(dcc_name):
        return ''
    if is_reference(dcc_name):
        return cmds.referenceQuery(dcc_name, filename=True, withoutCopyNumber=True)
    # Trying to find the attribute either on the transform or on the shapes
    if cmds.attributeQuery(REFERENCE_ATTRIBUTE_NAME, node=dcc_name, exists=True):
        return cmds.getAttr('{}.{}'.format(dcc_name, REFERENCE_ATTRIBUTE_NAME))
    shapes = cmds.listRelatives(dcc_name, f=True, shapes=1)
    if shapes:
        for shape in shapes:
            if cmds.attributeQuery(REFERENCE_ATTRIBUTE_NAME, node=shape, exists=True):
                return cmds.getAttr('{}.{}'.format(shape, REFERENCE_ATTRIBUTE_NAME))
        else:
            # If that's not a group - return the formatted name to be able to duplicate this mesh
            return node_to_promethean_name(dcc_name)
    return ''

@one_undo_chunk
def drop_asset(asset_paths, is_promethean_asset=True):
    global units_multiplier
    # dropping the asset path into the viewport and raytracing it to the mouse cursor

    if not asset_paths:
        return

    panel = cmds.getPanel(underPointer=True) or cmds.getPanel(wf=True)
    if not panel in cmds.getPanel(type="modelPanel"):
        return

    # Taking the mouse position to load the asset
    view = omui1.M3dView()
    omui1.M3dView.getM3dViewFromModelEditor(panel, view)
    source_point = om1.MPoint(0, 0, 0)
    direction = om1.MVector(0, 0, 0)
    view_height = view.portHeight()
    pos = QCursor.pos()
    view_widget_ptr = view.widget()
    view_widget = wrapInstance(int(view_widget_ptr), QWidget)
    rel_pos = view_widget.mapFromGlobal(pos)
    view.viewToWorld(int(rel_pos.x()), int(view_height - rel_pos.y()), source_point, direction)
    # convert to current units from centimeters
    source_point /= units_multiplier
    hit_object, hit_position, hit_normal = raytrace([source_point.x,source_point.y, source_point.z], [direction.x,direction.y, direction.z])

    world_rotation = None
    if hit_object:
        world_rotation = cmds.angleBetween(euler=True, v1=(0.0, 1.0, 0.0), v2=hit_normal)
        world_position = hit_position
    else:
        try:
            world_position = cmds.autoPlace(useMouse=True)
            # autoPlace function for some reason uses cm instead of current units, so we just use our global multiplier to convert
            world_position = [x / units_multiplier for x in world_position]
        # Sometimes Maya's command just errors out not able to find the point(too big negative Y for example)
        except RuntimeError:
            world_position = [0, 0, 0]

    def _get_pivot_offset(bbox):
        x = bbox[0][0] + (bbox[0][1] - bbox[0][0]) / 2.0
        y = bbox[1][0]
        z = bbox[2][0] + (bbox[2][1] - bbox[2][0]) / 2.0
        return [x, y, z]

    # Create objects and snap each of them to the surface
    uuids = []
    offsets = []
    widths = []
    for i, asset_path in enumerate(asset_paths):
        new_transforms = reference_asset(asset_path, is_promethean_asset=is_promethean_asset)
        uuids.append(cmds.ls(new_transforms, uuid=True))
        meshes = cmds.listRelatives(new_transforms, allDescendents=1, type="mesh", f=1)
        if meshes:
            bbox = cmds.polyEvaluate(meshes, b=1)
        else:
            bbox = ((0, 100 / units_multiplier), (0, 100 / units_multiplier), (0, 100 / units_multiplier))
        widths.append(bbox[0][1] - bbox[0][0])
        # That's the offset between bottom center and the object position
        offsets.append(_get_pivot_offset(bbox))

    total_width = sum(widths)
    current_width = 0
    transforms_list = [cmds.ls(uuid_list, l=True) for uuid_list in uuids]
    # Flatten the list
    all_transforms = [transform for sublist in transforms_list for transform in sublist]
    for i, transforms in enumerate(transforms_list):
        # Space out in X based on the width
        new_position = [-total_width/2 + current_width + widths[i] / 2 + world_position[0] - offsets[i][0],
                        world_position[1] - offsets[i][1],
                        world_position[2] - offsets[i][2]]
        current_width += widths[i]
        # Raytrace on the surface
        hit_object, hit_position, hit_normal = raytrace(new_position, [0.0, -1.0, 0.0], ignore_dcc_names=all_transforms)
        new_rotation = [0.0, 0.0, 0.0]
        if hit_object:
            new_position = hit_position
            # TODO: maybe have some threshold for the angle?
            new_rotation = cmds.angleBetween(euler=True, v1=(0.0, 1.0, 0.0), v2=hit_normal)
        cmds.xform(transforms, t=new_position, ro=new_rotation, ws=1)
# =====================================================================
# +++ Maya Raytrace
# =====================================================================

def raytrace(start_point, direction, distance=9999999999, ignore_dcc_names=()):
    # We consider that all the inputs are in the current unit system
    global units_multiplier
    results = list()

    #TODO: consider not only meshes in the viewport, but all instead?
    meshes_in_view = [x for x in get_geometry_in_view() if x not in ignore_dcc_names]
    if not meshes_in_view:
        return None, None, None

    panel = cmds.getPanel(underPointer=True) or cmds.getPanel(wf=True)
    if 'modelPanel' not in panel:
        return None, None, None

    view = omui1.M3dView()
    omui1.M3dView.getM3dViewFromModelEditor(panel, view)

    # convert inputs to centimeters from current units
    source_point = om1.MFloatPoint(*start_point) * units_multiplier
    distance *= units_multiplier

    direction = om1.MFloatVector(*direction)

    for mesh in meshes_in_view:

        hit_face = om1.MScriptUtil()
        hit_face.createFromInt(0)
        hit_point = om1.MFloatPoint()
        hit_face_ptr = hit_face.asIntPtr()
        hit_distance = om1.MScriptUtil(0.0)
        hit_distance_ptr = hit_distance.asFloatPtr()

        sel_list = om1.MSelectionList()
        sel_list.add(mesh)
        dag_path = om1.MDagPath()
        sel_list.getDagPath(0, dag_path)
        mesh = om1.MFnMesh(dag_path)
        intersected = mesh.closestIntersection(
            source_point, direction, None, None, False, om1.MSpace.kWorld, int(distance), True, None, hit_point,
            hit_distance_ptr, hit_face_ptr, None, None, None, 0.0001)
        if intersected:
            hit_object = dag_path.fullPathName()
            hit_normal = om1.MVector()
            mesh.getClosestNormal(om1.MPoint(hit_point), hit_normal, om1.MSpace.kWorld)
            results.append((hit_object, hit_point, hit_normal))

    hit_distance = None
    closest_hit = None
    for hit in results:
        if closest_hit is None:
            closest_hit = hit
            hit_distance = hit[1].distanceTo(source_point)
        else:
            hit_dst = hit[1].distanceTo(source_point)
            if hit_dst < hit_distance:
                hit_distance = hit_dst
                closest_hit = hit
    if not closest_hit:
        return None, None, None

    hit_object = closest_hit[0]
    # convert output from centimeters to current units
    hit_position = [closest_hit[1].x / units_multiplier, closest_hit[1].y / units_multiplier, closest_hit[1].z / units_multiplier]
    hit_normal = closest_hit[2].normal()
    hit_normal = [hit_normal.x, hit_normal.y, hit_normal.z]
    return hit_object, hit_position, hit_normal

def find_close_intersection_point(root_node, direction, distance=9999999999):
    source_world_position = cmds.xform(root_node, ws=True, t=True, q=True)
    # Raytrace both ways from the node position
    start_point = [source_pos - dir * distance * 0.5 for source_pos,dir in zip(source_world_position, direction)]
    return raytrace(source_world_position, direction, distance, ignore_dcc_names=[root_node])

def snap_to_cursor():
    global _snap_active
    if not _snap_active:
        return
    if not QApplication.mouseButtons() & (QtCore.Qt.LeftButton | QtCore.Qt.MiddleButton):
        return
    if QApplication.keyboardModifiers() & QtCore.Qt.AltModifier:
        return


    current_selection = cmds.ls(sl=True, tr=True, long=1)
    if not current_selection:
        return

    # Only snap if move tool is enabled
    if not cmds.contextInfo(cmds.currentCtx(), c=1) == 'manipMove':
        return

    # Only snap in tweak mode or moving in all 3 axis (probably the first is always true if the second)
    if not cmds.manipMoveContext('Move', q=1, currentActiveHandle=1) == 3 and \
            not cmds.manipMoveContext('Move', q=1, tweakMode=1):
        return

    selected_node = current_selection[0]

    # Make sure we are in viewport or it's in focus
    panel = cmds.getPanel(underPointer=True) or cmds.getPanel(wf=True)
    if not panel in cmds.getPanel(type="modelPanel"):
        return

    # Raytracing from camera to the object and see intersection
    current_selection_position = cmds.xform(selected_node, ws=True, t=True, q=True)
    camera = cmds.modelEditor(panel, q=1, av=1, cam=1)
    camera_position = cmds.xform(camera, ws=True, t=True, q=True)
    direction = [a - b for a, b in zip(current_selection_position, camera_position)]
    hit_object, hit_position, hit_normal = raytrace(camera_position, direction, ignore_dcc_names=current_selection)

    # TODO: this solution is preferable, but need to figure out what to do with selection
    #  (objects are moved to the new selection)
    # # Taking the current view and get mouse position as the source point and raytrace it to the world
    # view = omui1.M3dView()
    # omui1.M3dView.getM3dViewFromModelEditor(panel, view)
    # source_point = om1.MPoint(0, 0, 0)
    # direction = om1.MVector(0, 0, 0)
    # view_height = view.portHeight()
    # pos = QCursor.pos()
    # view_widget_ptr = view.widget()
    # view_widget = wrapInstance(int(view_widget_ptr), QtWidgets.QWidget)
    # rel_pos = view_widget.mapFromGlobal(pos)
    # view.viewToWorld(int(rel_pos.x()), int(view_height - rel_pos.y()), source_point, direction)
    #
    # new_position = raytrace([source_point.x,source_point.y, source_point.z], [direction.x,direction.y, direction.z],
    #                         ignore_dcc_names=current_selection)

    if hit_object:
        original_rotation = cmds.xform(selected_node, q=True, ro=True)
        rotation = cmds.angleBetween(euler=True, v1=(0.0, 1.0, 0.0), v2=hit_normal)
        cmds.xform(selected_node, ws=True, t=hit_position, ro=rotation)
        cmds.rotate(original_rotation[1], selected_node, ws=False, rotateY=True)


_snap_timer.timeout.connect(snap_to_cursor)
_snap_timer.start()

def translate_and_raytrace_by_name(dcc_names, location, raytrace_distance, max_normal_deviation, ignore_dcc_names):
    for dcc_name in dcc_names:
        start_point = location
        if raytrace_distance:
            hit_object, hit_position, hit_normal = raytrace(start_point, [0, -1, 0], raytrace_distance, ignore_dcc_names=ignore_dcc_names)
            if hit_object:
                location = hit_position
                up_dot_product = dot([0, 1, 0], hit_normal)
                # If we pass the threshold, align the objects to the normal
                if up_dot_product > max_normal_deviation:
                    rotation = cmds.angleBetween(euler=True, v1=(0.0, 1.0, 0.0), v2=hit_normal)
                    cmds.xform(dcc_name, ws=True, ro=rotation)
        cmds.move(location[0], location[1], location[2], dcc_name, absolute=True)

        # TODO: align the object with the normal of the intersection

# =====================================================================
# +++ Maya BULLET
# =====================================================================

DEFAULT_STATIC_NODE_NAMES = ['floor', 'terrain']


def set_as_dynamic_object(node):
    if not node:
        return None

    orig_sel = cmds.ls(sl=True, long=True)

    rb = RigidBody.CreateRigidBody()
    option_var_dict = rb.getOptionVars()
    option_var_dict_with_defaults = rb.optionVarDefaults.copy()
    option_var_dict_with_defaults.update(option_var_dict)
    option_var_dict_with_defaults['colliderShapeType'] = RigidBody.eShapeType.kColliderCompound
    option_var_dict_with_defaults['bodyType'] = RigidBody.eBodyType.kDynamicRigidBody

    cmds.select(node, replace=True)

    new_rigid_body = RigidBody.CreateRigidBody().command(**option_var_dict_with_defaults)

    if orig_sel:
        cmds.select(orig_sel)

    return new_rigid_body


def set_as_static_object(node):
    if not node:
        return None

    orig_sel = cmds.ls(sl=True, long=True)

    rb = RigidBody.CreateRigidBody()
    option_var_dict = rb.getOptionVars()
    option_var_dict_with_defaults = rb.optionVarDefaults.copy()
    option_var_dict_with_defaults.update(option_var_dict)
    option_var_dict_with_defaults['colliderShapeType'] = RigidBody.eShapeType.kColliderCompound
    option_var_dict_with_defaults['bodyType'] = RigidBody.eBodyType.kStaticBody

    cmds.select(node, replace=True)

    new_static_body = RigidBody.CreateRigidBody().command(**option_var_dict_with_defaults)

    if orig_sel:
        cmds.select(orig_sel)

    return new_static_body


def get_potential_static_nodes(dynamic_nodes=None):
    static_nodes = list()

    # 1) Nodes that are in the default list of static nodes
    dynamic_nodes = dynamic_nodes or list()
    dynamic_nodes = list(set([cmds.ls(node, shortNames=True)[0] for node in dynamic_nodes]))
    for default_name in DEFAULT_STATIC_NODE_NAMES:
        if default_name in dynamic_nodes or not cmds.objExists(default_name):
            continue
        static_nodes.append(default_name)

    # 2) Nodes that are visible
    geo_in_view = [node for node in get_geometry_in_view() if cmds.ls(
        node, shortNames=True)[0] not in dynamic_nodes and cmds.ls(node, shortNames=True)[0] not in static_nodes]
    static_nodes.extend(geo_in_view)

    return static_nodes


def merge_geometry(node):
    main_parent = cmds.listRelatives(node, p=1, f=1)
    world_pos = cmds.xform(node, q=True, t=True, ws=1)
    transform_children = cmds.listRelatives(node, allDescendents=1, type="transform", f=1)
    if transform_children:
        transform_children.append(node)
        mesh_children = [cmds.ls(x, uuid=True)[0] for x in transform_children if cmds.listRelatives(x, children=1, type="mesh", f=1)]
        non_mesh_children = [cmds.ls(x, uuid=True)[0] for x in transform_children if cmds.ls(x, uuid=True)[0] not in mesh_children]
        if mesh_children:
            cmds.parent(transform_children, w=1)
            if len(mesh_children) > 1:
                unite_obj = cmds.polyUnite(cmds.ls(mesh_children, l=True), name="tmp_Unite_object", constructionHistory=0)
            else:
                unite_obj = cmds.ls(mesh_children[0], l=True)
            if main_parent:
                unite_obj = cmds.parent(unite_obj, main_parent)
            unite_obj = cmds.rename(unite_obj, node.split("|")[-1])
            cmds.xform(unite_obj, pivots=world_pos)  # fix pivot if it gets reset to origin
            for uuid in non_mesh_children:
                node_to_delete = cmds.ls(uuid, l=True)
                cmds.delete(node_to_delete)
            return unite_obj
        return node
    else:
        return node


def get_unique_file_path(desired_path):
    """
    Finds the file path that doesn't exist yet in the same folder as the desired path
    :param desired_path: path that you'd use in case there are no files in the destination folder
    """
    if not os.path.exists(desired_path):
        return desired_path
    file_name, extension = os.path.splitext(os.path.basename(desired_path))
    folder = os.path.dirname(desired_path)
    new_file_path = ''
    i = 1
    while True:
        new_file_path = os.path.join(folder, '{}-{}{}'.format(file_name, i, extension))
        if not os.path.exists(new_file_path):
            break
        i += 1
    return os.path.normpath(new_file_path)