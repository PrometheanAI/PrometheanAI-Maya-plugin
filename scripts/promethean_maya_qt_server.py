import os
import sys
import traceback


try:
    from PySide2.QtCore import QObject
    from PySide2.QtWidgets import QApplication
    from PySide2.QtNetwork import QTcpServer, QHostAddress, QTcpSocket
except ImportError:
    from PySide.QtCore import QObject
    from PySide.QtGui import QApplication
    from PySide.QtNetwork import QTcpServer, QHostAddress, QTcpSocket

# For autocompletion
if False:
    from PyQt5 import QtCore
    from PyQt5 import QtWidgets
    from PyQt5 import QtNetwork

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import promethean_maya
import promethean_maya_version


class MayaServer(QObject, object):
    PORT = 1234

    def __init__(self, parent=None):
        parent = parent or QApplication.activeWindow()
        super(MayaServer, self).__init__(parent)

        self._socket = None
        self._server = None
        self._port = self.__class__.PORT

        self.connect()

    def connect(self, try_disconnect=True):
        self._server = QTcpServer(self)
        self._server.newConnection.connect(self._on_established_connection)
        if self._listen():
            print(
                'PrometheanAI {}: Maya server listening on port: {}'.format(
                    promethean_maya_version.version, self._port))
        else:
            if try_disconnect:
                self.disconnect()
                self.connect(try_disconnect=False)
                return
            print('PrometheanAI: Maya server initialization failed. If the problem persists, restart Maya please.')

    def disconnect(self):
        if self._socket:
            # self._socket.disconnected.disconnect()
            self._socket.readyRead.disconnect()
            self._socket.close()
            self._socket.deleteLater()
            self._socket = None

        self._server.close()
        print('PrometheanAI: Maya server connection disconnected')

    def _listen(self):
        if not self._server.isListening():
            return self._server.listen(QHostAddress.LocalHost, self._port)

        return False

    def _read(self):

        bytes_remaining = -1

        while self._socket.bytesAvailable():
            if bytes_remaining <= 0:
                byte_array = self._socket.read(131072)
                data = byte_array.data().decode('utf-8') if byte_array else ''
                self._process_data(data)

    def _process_data(self, data_str):
        data_commands = data_str.split('\n')
        res = None
        while data_commands:
            data = data_commands.pop(0)
            if not data:
                continue
            print('PrometheanAI: New network Maya command: {}'.format(data))

            try:
                res = promethean_maya.command_switch(data)
            except Exception as exc:
                print('PrometheanAI: Maya command switch failed with this message:')
                traceback.print_exc()
                # make sure to send the reply back to Promethean as otherwise it may freeze
                res = 'None'
        # we return just the result of last command, cause that's what Promethean may wait from the plugin
        if res:
            print('PrometheanAI: Sending message back to Promethean: {}'.format(res.encode('utf-8')))
            self._write(res)

    def _write(self, reply_str):
        if self._socket and self._socket.state() == QTcpSocket.ConnectedState:
            data = reply_str.encode()
            self._socket.write(data)

        return reply_str

    def _write_error(self, error_msg):
        print(error_msg)

    def _on_established_connection(self):
        self._socket = self._server.nextPendingConnection()
        if self._socket.state() == QTcpSocket.ConnectedState:
            # self._socket.disconnected.connect(self._on_disconnected)
            self._socket.readyRead.connect(self._read)
            print('PrometheanAI: Connection established')

    def _on_disconnected(self):
        self.disconnect()


if __name__ == '__main__':

    try:
        server.disconnect()
    except Exception:
        pass

    server = MayaServer()
