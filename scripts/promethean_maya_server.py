__author__ = 'Andrew Maximov info@prometheanai.com'
import time
import logging
import socket
import threading
from functools import partial
import traceback
import promethean_maya
import promethean_maya_version

import maya.cmds as cmds

# =====================================================================
# +++ GLOBALS
# =====================================================================
server_socket = None
enable_command_stack = True
command_stack = []
host = "127.0.0.1"
port = 1234
vacate_socket_command = 'promethean_vacate_socket'
ignore_vacate_socket = False


# =====================================================================
# +++ MAYA TCP SERVER SETUP
# =====================================================================
def start_server():
    close_server()  # to knock out existing connections we send a vacate socket command on start
    time.sleep(0.2)

    global enable_command_stack
    enable_command_stack = True

    global server_socket
    server_socket = socket.socket()
    server_socket.bind((host, port))
    server_socket.listen(5)

    threading.Thread(target=server_thread).start()
    threading.Thread(target=execute_command_stack).start()
    print('PrometheanAI {}: Server Started'.format(promethean_maya_version.version))


def server_thread():
    global server_socket
    global command_stack

    def connection_thread(connection_):
        while True:
            try:
                data = connection_.recv(131072)  # 4096
                if data:
                    if data.decode() == vacate_socket_command:
                        if not ignore_vacate_socket:
                            print("PrometheanAI: Received a Vacate Socket Command. Disconnecting")
                            close_server()  # allow other servers start ups to close this down
                    else:
                        command_stack.append((data, connection_))  # bytes to unicode to string
                else:
                    break
            except:
                pass

    while server_socket:
        try:
            connection, addr = server_socket.accept()  # will wait to get the connection so we are not constantly looping
            if enable_command_stack:  # connection could have been closed while we were still listening
                print("PrometheanAI: Got connection from " + str(addr))
                threading.Thread(target=partial(connection_thread, connection)).start()
        except:
            server_socket = None  # if getting connection crashed the socket could have been disconnected


def close_server():
    # - close socket connection
    global server_socket
    if server_socket:
        server_socket.close()
        server_socket = None  # this will exit the server loop
    # - stop command stack
    global enable_command_stack
    enable_command_stack = False

    # - Warning! The socket.accept() command is still hanging in the server thread so sending a message to close it down
    send_vacate_socket_command()  # but also this will knock out any other instances of the server occupying the socket


def execute_command_stack():
    time.sleep(0.001)  # waiting for socket to set up
    while enable_command_stack:
        if command_stack:
            data, connection = command_stack.pop(0)
            print('PrometheanAI: New network command stack item: %s' % data)
            try:  # make sure a crash doesn't stop the stack from being read
                with Undo():
                    promethean_maya.command_switch(connection, str(data.decode()))  # bytes to string
            except Exception as e:
                print('PrometheanAI: Maya command switch failed with this message:')
                traceback.print_exc()

            connection.close()
        else:
            time.sleep(0.001)


def send_vacate_socket_command():
    """ when starting a new server we can send this message to make sure whatever maya has the connection now
        releases it """
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:  # in case no one is listening
        client_socket.connect((host, port))
        client_socket.send(vacate_socket_command.encode())
        client_socket.close()
    except:
        pass


class Undo(object):
    def __enter__(self):
        cmds.undoInfo(openChunk=True)

    def __exit__(self, *exc_info):
        cmds.undoInfo(closeChunk=True)