from datetime import datetime

from thingsboard_gateway.connectors.modbus.crc import calculator, modbus_crc16
from thingsboard_gateway.connectors.socket.socket_uplink_converter import SocketUplinkConverter


def get_cmd(data_hex):
    if len(data_hex) == 0:
        return None
    if 0 < len(data_hex) < 4:
        return None
    return data_hex[4:6]


def get_msg_body(data_hex):
    if len(data_hex) == 0:
        return None
    if 0 < len(data_hex) < 4:
        return None
    return data_hex[6:-4]


def get_msg_crc(data_hex):
    if len(data_hex) == 0:
        return None
    return data_hex[-8:-4]


def acrel_crc(hex_str):
    """
    安科瑞MODBUS CRC 校验需要调整CRC校验码的位置
    """
    out = calculator(hex_str, modbus_crc16).replace('0x', '')
    # before = out[0:2]
    # after = out[2:]
    # return after + before
    return out


def get_time_cmd():
    ts = datetime.now().strftime('%y%m%d0%w%H%M%S')
    ts_list = list()
    for i in range(0, int(len(ts) / 2)):
        ts_list.append(str("%02x" % int(ts[i * 2: i * 2 + 2], 16)))
    return ''.join(ts_list)


class BytesSocketUplinkArclConverter(SocketUplinkConverter):

    def __init__(self, config, logger):
        self._log = logger
        self.__config = config

    def convert(self, config, data):
        if data is None:
            return {}
