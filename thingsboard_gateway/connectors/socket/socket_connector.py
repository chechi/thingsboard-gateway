#     Copyright 2024. ThingsBoard
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.
import binascii
import socket
from queue import Queue
from random import choice
from re import findall, compile, fullmatch
from string import ascii_lowercase
from threading import Thread
from time import sleep

from simplejson import dumps

from thingsboard_gateway.connectors.connector import Connector
from thingsboard_gateway.connectors.socket.socket_decorators import CustomCollectStatistics
from thingsboard_gateway.extensions.socket.socket_uplink_acrel import get_cmd, get_msg_body, get_msg_crc, get_time_cmd, \
    acrel_crc
from thingsboard_gateway.gateway.statistics_service import StatisticsService
from thingsboard_gateway.tb_utility.tb_loader import TBModuleLoader
from thingsboard_gateway.tb_utility.tb_logger import init_logger

SOCKET_TYPE = {
    'TCP': socket.SOCK_STREAM,
    'UDP': socket.SOCK_DGRAM
}
DEFAULT_UPLINK_CONVERTER = 'BytesSocketUplinkConverter'


def response(connection, cmd, directive):
    if len(cmd['response']) > 0:
        if directive == '84':
            connection.send(b'\x7B\x7B\x84\xBF\x23\x7D\x7D')
            return


class SocketConnector(Connector, Thread):
    def __init__(self, gateway, config, connector_type):
        super().__init__()
        self.__config = config
        self.__id = self.__config.get('id')
        self._connector_type = connector_type
        self.statistics = {'MessagesReceived': 0,
                           'MessagesSent': 0}
        self.__gateway = gateway
        self.name = config.get("name", 'TCP Connector ' + ''.join(choice(ascii_lowercase) for _ in range(5)))
        self.__log = init_logger(self.__gateway, self.name, self.__config.get('logLevel', 'INFO'),
                                 enable_remote_logging=self.__config.get('enableRemoteLogging', False))
        self.daemon = True
        self.__stopped = False
        self._connected = False
        self.__bind = False
        self.__socket_type = config['type'].upper()
        self.__socket_address = config['address']
        self.__socket_port = config['port']
        self.__socket_buff_size = config['bufferSize']
        self.__socket = socket.socket(socket.AF_INET, SOCKET_TYPE[self.__socket_type])
        self.__socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.__converting_requests = Queue(-1)
        self.__devices_config = self.__config.get('devices', [])
        self.__devices, self.__device_converters = self.__convert_devices_list()
        self.__connections = {}

    # 转换设备列表
    def __convert_devices_list(self):
        devices = self.__config.get('devices', [])

        converted_devices = {}
        converters_for_devices = {}
        for device in devices:
            address = device.get('addressFilter', device.get('address', None))
            if address is None:
                self.__log.error('Device %s has no addressFilter or address', device.get('deviceName', 'Unknown'))
                continue

            address = address.replace('*', '.*')
            address_key = address
            try:
                address_key = compile(address)
            except Exception as e:
                self.__log.debug("Cannot compile device address with regex! %r", e)

            module = self.__load_converter(device)
            # 加载解码器
            converter = module(device, self.__log) if module else None

            converters_for_devices[address_key] = converter

            # validate attributeRequests requestExpression
            attr_requests = device.get('attributeRequests', [])
            device['attributeRequests'] = self.__validate_attr_requests(attr_requests)
            if len(device['attributeRequests']):
                self.__attribute_type = {
                    'client': self.__gateway.tb_client.client.gw_request_client_attributes,
                    'shared': self.__gateway.tb_client.client.gw_request_shared_attributes
                }
            converted_devices[address_key] = device

        return converted_devices, converters_for_devices

    # 加载解码器
    def __load_converter(self, device):
        converter_class_name = device.get('converter', DEFAULT_UPLINK_CONVERTER)
        module = TBModuleLoader.import_module(self._connector_type, converter_class_name)

        if module:
            self.__log.debug('Converter %s for device %s - found!', converter_class_name, self.name)
            return module

        self.__log.error("Cannot find converter for %s device", self.name)
        return None

    def __validate_attr_requests(self, attr_requests):
        validated_attrs = []
        for attr in attr_requests:
            valid_attr = False
            if attr['requestExpression'] != '':
                if '${' in attr['requestExpression'] and '}' in attr['requestExpression']:
                    if '==' in attr['requestExpression']:
                        expression_arr = findall(r'\[[^\s][0-9:]*]', attr['requestExpression'])
                        if expression_arr:
                            indexes = expression_arr[0][1:-1].split(':')
                            if len(indexes) == 2:
                                from_index, to_index = indexes
                                attr['requestIndexFrom'] = from_index
                                attr['requestIndexTo'] = to_index
                            else:
                                attr['requestIndex'] = int(indexes[0])

                            attr['haveIndex'] = True
                            try:
                                attr['requestEqual'] = attr['requestExpression'].split('==')[-1][:-1]
                            except IndexError:
                                self.__log.error(f'{attr["requestExpression"]} not valid. Index out of range.')
                                continue

                            valid_attr = True
                            validated_attrs.append(attr)
                else:
                    valid_attr = True
                    attr['haveIndex'] = False
                    validated_attrs.append(attr)

            if not valid_attr:
                self.__log.error(f'{attr["requestExpression"]} not valid expression')

        return validated_attrs

    def open(self):
        self.__stopped = False
        self.start()

    def run(self):
        self._connected = True

        converting_thread = Thread(target=self.__process_data, daemon=True, name='Converter Thread')
        converting_thread.start()

        while not self.__bind:
            try:
                self.__socket.bind((self.__socket_address, self.__socket_port))
            except OSError:
                self.__log.error('Address already in use. Reconnecting...')
                sleep(3)
            else:
                self.__bind = True

        if self.__socket_type == 'TCP':
            self.__socket.listen(5)

        self.__log.info('%s SOCKET 服务启动成功', self.__socket_type)

        while not self.__stopped:
            try:
                if self.__socket_type == 'TCP':
                    try:
                        if self.__socket.fileno() != -1:  # Check if the socket is open
                            conn, address = self.__socket.accept()
                            self.__connections[address] = conn

                            self.__log.debug('新的网络连接 %s 接入', address)
                            thread = Thread(target=self.__process_tcp_connection, daemon=True,
                                            name=f'Processing {address} connection',
                                            args=(conn, address))
                            thread.start()
                    except OSError as e:
                        if self.__stopped:
                            break
                        self.__log.error('连接接入异常: %s', e)
                else:
                    data, client_address = self.__socket.recvfrom(self.__socket_buff_size)
                    self.__converting_requests.put((client_address, data))
            except ConnectionAbortedError:
                self.__socket.close()

    def response(self, connection, cmd, directive):
        if len(cmd['response']) > 0:
            if directive == '84':
                self.__log.debug('命令:{directive}:应答:{response}'.format(directive=directive,
                                                                           response='\x7B\x7B\x84\xBF\x23\x7D\x7D'))
                connection.send(b'\x7B\x7B\x84\xBF\x23\x7D\x7D')
                return

    def process_register_data(self, data_hex):
        self.__log.debug('处理注册信息')
        self.__log.debug('注册序列号:%s', data_hex[0:20])
        self.__log.debug('卡号:%s', data_hex[20:50])
        self.__log.debug('信号强度:%s', data_hex[50:51])
        self.__log.debug('固件版本1:%s', data_hex[51:53])
        self.__log.debug('固件版本2:%s', data_hex[53:55])
        self.__log.debug('固件版本3:%s', data_hex[55:57])
        self.__log.debug('定时上传间隔:%s', data_hex[57:58])

    def process_basic_data(self, data_hex):
        self.__log.debug('处理基础用电信息')
        self.__log.debug('IMEI号:%s', binascii.a2b_hex(data_hex[6:26]).decode('ascii'))

        self.__log.debug('A电压:%s', str(int(data_hex[70:74], 16) / 100))
        self.__log.debug('B电压:%s', str(int(data_hex[74:78], 16) / 100))
        self.__log.debug('C电压:%s', str(int(data_hex[78:82], 16) / 100))

        self.__log.debug('A电流:%s', int(data_hex[82:86], 16))
        self.__log.debug('B电流:%s', int(data_hex[86:90], 16))
        self.__log.debug('C电流:%s', int(data_hex[90:94], 16))

        self.__log.debug('A有功功率:%s', int(data_hex[94:98], 16))
        self.__log.debug('B有功功率:%s', int(data_hex[98:102], 16))
        self.__log.debug('C有功功率:%s', int(data_hex[102:106], 16))
        self.__log.debug('D有功功率:%s', int(data_hex[106:110], 16))

        self.__log.debug('A无功功率:%s', int(data_hex[110:114], 16))
        self.__log.debug('B无功功率:%s', int(data_hex[114:118], 16))
        self.__log.debug('C无功功率:%s', int(data_hex[118:122], 16))
        self.__log.debug('D无功功率:%s', int(data_hex[122:126], 16))

        self.__log.debug('A功率因数:%s', int(data_hex[126:130], 16))
        self.__log.debug('B功率因数:%s', int(data_hex[130:134], 16))
        self.__log.debug('C功率因数:%s', int(data_hex[134:138], 16))
        self.__log.debug('D功率因数:%s', int(data_hex[138:142], 16))

        self.__log.debug('PT:%s', int(data_hex[142:146], 16))
        self.__log.debug('CT:%s', int(data_hex[146:148], 16))

        self.__log.debug('总用电量:%s', int(data_hex[148:156], 16))
        self.__log.debug('尖用电量:%s', int(data_hex[156:164], 16))
        self.__log.debug('峰用电量:%s', int(data_hex[164:172], 16))
        self.__log.debug('平用电量:%s', int(data_hex[172:180], 16))
        self.__log.debug('谷用电量:%s', int(data_hex[180:192], 16))

        self.__log.debug('剩余金额:%s', int(data_hex[192:200], 16))
        self.__log.debug('购电次数:%s', int(data_hex[200:204], 16))

        self.__log.debug('{Y}年:{M}月:{D}日:星期{x}:{h}时:{m}分:{s}秒'.format(
            Y=str(int(data_hex[228:230], 16)),
            M=str(int(data_hex[230:232], 16)),
            D=str(int(data_hex[232:234], 16)),
            x=str(int(data_hex[234:236], 16)),
            h=str(int(data_hex[236:238], 16)),
            m=str(int(data_hex[238:240], 16)),
            s=str(int(data_hex[240:242], 16))
        ))

    def get_edit_time_cmd(self):

        cmd_msg = '93{cmd}'.format(cmd=get_time_cmd())

        self.__log.debug('对时下发命令消息体:%s', cmd_msg)

        crc = acrel_crc(cmd_msg)

        self.__log.debug('对时CRC校验码:%s', crc)

        send_msg = '7b7b' + cmd_msg + crc + '7d7d'

        self.__log.debug('发送的报文:%s', send_msg)

        return send_msg

    def get_edit_device_config_cmd(self):
        self.__log.debug('获取设备配置参数')

        cmd_msg = '8202'

        crc = acrel_crc(cmd_msg)

        self.__log.debug('CRC校验码:%s', crc)

        send_msg = '7b7b' + cmd_msg + crc + '7d7d'

        self.__log.debug('发送的报文:%s', send_msg)

        return send_msg

    def get_network_config_cmd(self):
        self.__log.debug('获取网关网络配置')

        # address = self.__config['address']
        address = '111.204.71.106'
        port = self.__config['port']

        self.__log.debug('{address}:{port}'.format(address=address, port=port))

        address_hex_list = []
        for ad in address.split('.'):
            address_hex_list.append(hex(int(ad)).replace('0x', ''))

        self.__log.debug('IP地址16进制编码:%s', address_hex_list)

        port_hex = hex(port).replace('0x', '')

        cmd_msg = '88' + ''.join(address_hex_list) + port_hex

        self.__log.debug('网络配置指令:%s', cmd_msg)

        crc = acrel_crc(cmd_msg)

        self.__log.debug('CRC校验码:%s', crc)

        send_msg = '7b7b' + cmd_msg + crc + '7d7d'

        self.__log.debug('发送的报文:%s', send_msg)

        return send_msg

    def __process_tcp_connection(self, connection, address):
        while not self.__stopped:
            data = connection.recv(self.__socket_buff_size)

            if data:
                self.__log.debug('接收到的原始报文转换为16进制数据:%s', data.hex())

                for device in self.__devices_config:
                    if device['category'] == 'acrel' and device['deviceName'] == 'ACREL-DDSY1352':

                        if len(data.hex()) == 0:
                            self.__log.warning('接收到的消息为空')
                            break

                        if get_msg_crc(data.hex()) is None:
                            self.__log.warning('缺失CRC校验码')
                            break

                        # if get_msg_crc(data.hex()) != acrel_crc(get_msg_body(data.hex())):
                        #     self.__log.debug('CRC校验失败')
                        #     break

                        # x84 注册请求
                        if get_cmd(data.hex()) == '84':
                            self.__log.debug('注册')

                            msg_body = get_msg_body(data.hex())
                            self.__log.debug('设备消息体:{msg_body}'.format(msg_body=msg_body))

                            self.process_register_data(msg_body)

                        # x84 注册响应
                        connection.send(bytes.fromhex('7b7b84bf237d7d'))

                        # x82 设置设备参数
                        connection.send(bytes.fromhex(self.get_edit_device_config_cmd()))

                        # x82 响应设备参数下发
                        if get_cmd(data.hex()) == '82':
                            self.__log.debug('修改设备参数')
                            msg_body = get_msg_body(data.hex())
                            self.__log.debug('设备消息体:{msg_body}'.format(msg_body=msg_body))

                        # x88 设置IP端口
                        connection.send(bytes.fromhex(self.get_network_config_cmd()))

                        # x88 响应服务端IP端口配置指令
                        if get_cmd(data.hex()) == '88':
                            self.__log.debug('接收设置IP端口信息')
                            msg_body = get_msg_body(data.hex())
                            self.__log.debug('设备消息体:{msg_body}'.format(msg_body=msg_body))

                        # x89 设备报警请求
                        if get_cmd(data.hex()) == '89':
                            self.__log.debug('报警')
                            msg_body = get_msg_body(data.hex())
                            self.__log.debug('设备消息体:{msg_body}'.format(msg_body=msg_body))

                        # x90 响应 modbus 数据透传
                        if get_cmd(data.hex()) == '90':
                            self.__log.debug('响应 modbus 数据透传')
                            msg_body = get_msg_body(data.hex())
                            self.__log.debug('设备消息体:{msg_body}'.format(msg_body=msg_body))

                        # x91 集抄用电信息
                        if get_cmd(data.hex()) == '91':
                            self.__log.debug('集抄用电信息')
                            msg_body = get_msg_body(data.hex())
                            self.__log.debug('设备消息体:{msg_body}'.format(msg_body=msg_body))
                            self.process_basic_data(msg_body)

                        # x93 设置对时
                        connection.send(bytes.fromhex(self.get_edit_time_cmd()))

                        # x93 设备响应对时
                        if get_cmd(data.hex()) == '93':
                            self.__log.debug('对时')
                            msg_body = get_msg_body(data.hex())
                            self.__log.debug('设备消息体:{msg_body}'.format(msg_body=msg_body))

                        # x94 设备心跳检测
                        if get_cmd(data.hex()) == '94':
                            self.__log.debug('心跳检测')
                            msg_body = get_msg_body(data.hex())
                            self.__log.debug('设备消息体:{msg_body}'.format(msg_body=msg_body))

                        # 97 ID模式的 Modbus透传 服务器下发含服务ID

                self.__converting_requests.put((address, data))
            else:
                break

        connection.close()
        self.__connections.pop(address)
        self.__log.debug('关闭 %s 链接', address)

    def __process_data(self):
        while not self.__stopped:
            if not self.__converting_requests.empty():
                (address, port), data = self.__converting_requests.get()
                for conf_device_address in self.__devices:
                    client_address = f"{address}:{port}"
                    if client_address != conf_device_address and not fullmatch(conf_device_address, client_address):
                        continue
                    device = self.__devices.get(conf_device_address)
                    device['address'] = client_address

                    # check data for attribute requests
                    is_attribute_request = False
                    attr_requests = device.get('attributeRequests', [])
                    if len(attr_requests):
                        for attr in attr_requests:
                            equal = data
                            if attr['haveIndex']:
                                if attr.get('requestIndexFrom') and attr.get('requestIndexTo'):
                                    index_from = int(attr['requestIndexFrom']) if attr[
                                                                                      'requestIndexFrom'] != '' else None
                                    index_to = int(attr['requestIndexTo']) if attr['requestIndexTo'] != '' else None
                                    equal = data[index_from:index_to]
                                else:
                                    equal = data[int(attr['requestIndex'])]

                            # if attr['requestEqual'] == equal.decode('utf-8'):
                            #     is_attribute_request = True
                            #     self.__process_attribute_request(device['deviceName'], attr, data)

                        if is_attribute_request:
                            continue

                    converter = self.__device_converters.get(conf_device_address)
                    self.__convert_data(device, data, converter)

            sleep(.2)

    def __convert_data(self, device, data, converter):
        address, port = device['address'].split(':')
        if not converter:
            self.__log.error('Converter not found for %s:%s', address, port)
            return

        try:
            device_config = {
                'deviceName': device.get('deviceName', None),
                'cmds': device.get('cmds', []),
                'encoding': device.get('encoding', 'utf-8').lower(),
                'telemetry': device.get('telemetry', []),
                'attributes': device.get('attributes', [])
            }
            # 解码器解码数据包
            converted_data = converter.convert(device_config, data)

            self.statistics['MessagesReceived'] = self.statistics['MessagesReceived'] + 1

            if converted_data is not None:
                self.__gateway.send_to_storage(self.get_name(), self.get_id(), converted_data)
                self.statistics['MessagesSent'] = self.statistics['MessagesSent'] + 1
                self.__log.info('Data to ThingsBoard %s', converted_data)
        except Exception as e:
            self.__log.exception(e)

    def __process_attribute_request(self, device_name, attr, data):
        expression_arr = findall(r'\[[^\s][0-9:]*]', attr['attributeNameExpression'])

        found_attributes = []
        if expression_arr:
            for exp in expression_arr:
                indexes = exp[1:-1].split(':')

                try:
                    if len(indexes) == 2:
                        from_index, to_index = indexes
                        attribute = data[int(from_index) if from_index != '' else None:int(
                            to_index) if to_index != '' else None]
                    else:
                        attribute = data[int(indexes[0])]

                    found_attributes.append(attribute.decode('utf-8'))
                except IndexError:
                    self.__log.error('Data length not valid due to attributeNameExpression')
                    return

        self.statistics['MessagesReceived'] = self.statistics['MessagesReceived'] + 1
        self.__attribute_type[attr['type']](device_name, found_attributes, self.__attribute_request_callback)

    def __attribute_request_callback(self, response, _):
        device = response.get('device')
        if not device:
            self.__log.error('Attribute request does\'t return device name')

        device = tuple(filter(lambda item: item['deviceName'] == device, self.__config['devices']))[0]
        address, port = device['address'].split(':')

        value = response.get('value') or response.get('values')
        converted_value = bytes(dumps(value), encoding='utf-8')

        if self.__socket_type == 'TCP':
            self.__write_value_via_tcp(address, port, converted_value)
        else:
            self.__write_value_via_udp(address, port, converted_value)
        self.statistics['MessagesSent'] = self.statistics['MessagesSent'] + 1

    def close(self):
        self.__stopped = True
        self._connected = False
        self.__log.info('%s connector has been stopped.', self.get_name())
        self.__connections = {}
        while self.__socket.fileno() != -1:
            try:
                self.__socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # Ignore errors when socket is already closed
            self.__socket.close()
            sleep(0.01)
        self.__log.stop()

    def get_name(self):
        return self.name

    def get_type(self):
        return self._connector_type

    def is_connected(self):
        return self._connected

    def is_stopped(self):
        return self.__stopped

    def get_config(self):
        return self.__config

    def get_id(self):
        return self.__id

    @CustomCollectStatistics(start_stat_type='allBytesSentToDevices')
    def __write_value_via_tcp(self, address, port, value):
        try:
            self.__connections[(address, int(port))].sendall(value)
            return 'ok'
        except KeyError:
            try:
                new_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                new_socket.connect((address, int(port)))
                new_socket.sendall(value)
                new_socket.close()
                return 'ok'
            except ConnectionRefusedError as e:
                self.__log.error('Can\'t connect to %s:%s\n %s', address, port, e)
                return e

    @staticmethod
    @StatisticsService.CollectStatistics(start_stat_type='allBytesSentToDevices')
    def __write_value_via_udp(address, port, value):
        new_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        new_socket.sendto(value, (address, int(port)))
        new_socket.close()

    @StatisticsService.CollectAllReceivedBytesStatistics(start_stat_type='allReceivedBytesFromTB')
    def on_attributes_update(self, content):
        try:
            device = tuple(filter(lambda item: item['deviceName'] == content['device'], self.__config['devices']))[0]

            for attribute_update_config in device['attributeUpdates']:
                for attribute_update in content['data']:
                    if attribute_update_config['attributeOnThingsBoard'] == attribute_update:
                        address, port = device['address'].split(':')
                        encoding = device.get('encoding', 'utf-8').lower()
                        converted_data = bytes(str(content['data'][attribute_update]), encoding=encoding)
                        self.__write_value_via_tcp(address, port, converted_data)
        except IndexError:
            self.__log.error('Device not found')

    @StatisticsService.CollectAllReceivedBytesStatistics(start_stat_type='allReceivedBytesFromTB')
    def server_side_rpc_handler(self, content):
        try:
            if content.get('data') is None:
                content['data'] = {'params': content['params'], 'method': content['method']}

            rpc_method = content['data']['method']

            # check if RPC type is connector RPC (can be only 'set')
            try:
                (connector_type, rpc_method_name) = rpc_method.split('_')
                if connector_type == self._connector_type:
                    rpc_method = rpc_method_name
                    content['device'] = content['params'].split(' ')[0].split('=')[-1]
            except (IndexError, ValueError):
                pass

            device = tuple(filter(lambda item: item['deviceName'] == content['device'], self.__config['devices']))[0]

            # check if RPC method is reserved set
            if rpc_method == 'set':
                params = {}
                for param in content['data']['params'].split(';'):
                    try:
                        (key, value) = param.split('=')
                    except ValueError:
                        continue

                    if key and value:
                        params[key] = value

                result = None
                try:
                    if self.__socket_type == 'TCP':
                        result = self.__write_value_via_tcp(params['address'], int(params['port']), params['value'])
                    else:
                        self.__write_value_via_udp(params['address'], int(params['port']), params['value'])
                except KeyError:
                    self.__gateway.send_rpc_reply(device=device, req_id=content['data'].get('id'),
                                                  content='Not enough params')
                except ValueError:
                    self.__gateway.send_rpc_reply(device=device, req_id=content['data']['id'],
                                                  content='Param "port" have to be int type')
                else:
                    self.__gateway.send_rpc_reply(device=device, req_id=content['data'].get('id'), content=str(result))
            else:
                for rpc_config in device['serverSideRpc']:
                    for (key, value) in content['data'].items():
                        if value == rpc_config['methodRPC']:
                            rpc_method = rpc_config['methodProcessing']
                            return_result = rpc_config['withResponse']
                            result = None

                            address, port = device['address'].split(':')
                            encoding = device.get('encoding', 'utf-8').lower()
                            converted_data = bytes(str(content['data']['params']), encoding=encoding)

                            if rpc_method.upper() == 'WRITE':
                                if self.__socket_type == 'TCP':
                                    result = self.__write_value_via_tcp(address, port, converted_data)
                                else:
                                    self.__write_value_via_udp(address, port, converted_data)

                            if return_result and self.__socket_type == 'TCP':
                                self.__gateway.send_rpc_reply(content['device'], content['data']['id'], str(result))

                            return
        except IndexError:
            self.__log.error('Device not found')
        except Exception as e:
            self.__log.exception(e)
