__author__ = 'Andrew Maximov info@prometheanai.com'
import socket

# =====================================================================
# +++ CONVENIENCE FUNCTIONS TO SEND TCP MESSAGES
# =====================================================================
host = "127.0.0.1"


def send_message(msg, target=None):
    port = 1234  # - maya is default
    if type(target) is str:
        if target == 'browser':
            port = 1312
        elif target == 'cmd_line':
            port = 1313
    elif type(target) is int:
        port = target

    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:  # in case no one is listening
        client_socket.connect((host, port))
        client_socket.send(msg.encode())
        client_socket.close()
    except:
        pass


def send_message_and_get_reply(msg, target=None):
    port = 1234  # - maya is default
    if type(target) is str:
        if target == 'browser':
            port = 1312
        elif target == 'cmd_line':
            port = 1313
    elif type(target) is int:
        port = target

    timeout_time = 180
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:  # in case no one is listening
        client_socket.connect((host, port))
        client_socket.send(msg.encode())
        client_socket.settimeout(timeout_time)
        client_socket.shutdown(socket.SHUT_WR)
        result = client_socket.recv(2097152)
        client_socket.close()
        return result.decode()
    except:
        return False


if __name__ == '__main__':
    # - test tcp messages here
    # send_message('get_scene_name')
    # send_message('set_selected_by_path mesh Levels/Asgard/_RealmModels/Props/Animals/asg_pigpen01/asg_pigpen01.mb', 'browser')
    # send_message('set_selected_by_path material t1/objects/characters-t1x/npc-normal/parts-legs/m-pnt-01/m-pnt-01-u1-s1','browser')
    print(send_message_and_get_reply('get_p4_data'))

