#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2015-2015 breakwa11
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from __future__ import absolute_import, division, print_function, \
    with_statement

import hashlib
import logging
import binascii
import base64
import time
import datetime
import random
import math
import struct
import hmac
import bisect

import shadowsocks
from shadowsocks import common, lru_cache, encrypt
from shadowsocks.obfsplugin import plain
from shadowsocks.common import to_bytes, to_str, ord, chr
from shadowsocks.crypto import openssl

rand_bytes = openssl.rand_bytes

def create_auth_chain_a(method):
    return auth_chain_a(method)


def create_auth_chain_b(method):
    return auth_chain_b(method)


def create_auth_chain_c(method):
    return auth_chain_c(method)


def create_auth_chain_d(method):
    return auth_chain_d(method)


def create_auth_chain_e(method):
    return auth_chain_e(method)


def create_auth_chain_f(method):
    return auth_chain_f(method)


obfs_map = {
    'auth_chain_a': (create_auth_chain_a,),
    'auth_chain_b': (create_auth_chain_b,),
    'auth_chain_c': (create_auth_chain_c,),
    'auth_chain_d': (create_auth_chain_d,),
    'auth_chain_e': (create_auth_chain_e,),
    'auth_chain_f': (create_auth_chain_f,),
}


class xorshift128plus(object):
    max_int = (1 << 64) - 1
    mov_mask = (1 << (64 - 23)) - 1

    def __init__(self):
        self.v0 = 0
        self.v1 = 0

    def next(self):
        x = self.v0
        y = self.v1
        self.v0 = y
        x ^= ((x & xorshift128plus.mov_mask) << 23)
        x ^= (y ^ (x >> 17) ^ (y >> 26))
        self.v1 = x
        return (x + y) & xorshift128plus.max_int

    def init_from_bin(self, bin):
        if len(bin) < 16:
            bin += b'\0' * 16
        self.v0 = struct.unpack('<Q', bin[:8])[0]
        self.v1 = struct.unpack('<Q', bin[8:16])[0]

    def init_from_bin_len(self, bin, length):
        if len(bin) < 16:
            bin += b'\0' * 16
        self.v0 = struct.unpack('<Q', struct.pack('<H', length) + bin[2:8])[0]
        self.v1 = struct.unpack('<Q', bin[8:16])[0]

        for i in range(4):
            self.next()

def match_begin(str1, str2):
    if len(str1) >= len(str2):
        if str1[:len(str2)] == str2:
            return True
    return False


class auth_base(plain.plain):
    def __init__(self, method):
        super(auth_base, self).__init__(method)
        self.method = method
        self.no_compatible_method = ''
        self.overhead = 4

    def init_data(self):
        return ''

    def get_overhead(self, direction):  # direction: true for c->s false for s->c
        return self.overhead

    def set_server_info(self, server_info):
        self.server_info = server_info

    def client_encode(self, buf):
        return buf

    def client_decode(self, buf):
        return (buf, False)

    def server_encode(self, buf):
        return buf

    def server_decode(self, buf):
        return (buf, True, False)

    def not_match_return(self, buf):
        self.raw_trans = True
        self.overhead = 0
        if self.method == self.no_compatible_method:
            return (b'E' * 2048, False)
        return (buf, False)


class client_queue(object):
    def __init__(self, begin_id):
        self.front = begin_id - 64
        self.back = begin_id + 1
        self.alloc = {}
        self.enable = True
        self.last_update = time.time()
        self.ref = 0

    def update(self):
        self.last_update = time.time()

    def addref(self):
        self.ref += 1

    def delref(self):
        if self.ref > 0:
            self.ref -= 1

    def is_active(self):
        return (self.ref > 0) and (time.time() - self.last_update < 60 * 10)

    def re_enable(self, connection_id):
        self.enable = True
        self.front = connection_id - 64
        self.back = connection_id + 1
        self.alloc = {}

    def insert(self, connection_id):
        if not self.enable:
            logging.warn('obfs auth: not enable')
            return False
        if not self.is_active():
            self.re_enable(connection_id)
        self.update()
        if connection_id < self.front:
            logging.warn('obfs auth: deprecated id, someone replay attack')
            return False
        if connection_id > self.front + 0x4000:
            logging.warn('obfs auth: wrong id')
            return False
        if connection_id in self.alloc:
            logging.warn('obfs auth: duplicate id, someone replay attack')
            return False
        if self.back <= connection_id:
            self.back = connection_id + 1
        self.alloc[connection_id] = 1
        while (self.front in self.alloc) or self.front + 0x1000 < self.back:
            if self.front in self.alloc:
                del self.alloc[self.front]
            self.front += 1
        self.addref()
        return True


class obfs_auth_chain_data(object):
    def __init__(self, name):
        self.name = name
        self.user_id = {}
        self.local_client_id = b''
        self.connection_id = 0
        self.set_max_client(64)  # max active client count

    def update(self, user_id, client_id, connection_id):
        if user_id not in self.user_id:
            self.user_id[user_id] = lru_cache.LRUCache()
        local_client_id = self.user_id[user_id]

        if client_id in local_client_id:
            local_client_id[client_id].update()

    def set_max_client(self, max_client):
        self.max_client = max_client
        self.max_buffer = max(self.max_client * 2, 1024)

    def insert(self, user_id, client_id, connection_id):
        if user_id not in self.user_id:
            self.user_id[user_id] = lru_cache.LRUCache()
        local_client_id = self.user_id[user_id]

        if local_client_id.get(client_id, None) is None or not local_client_id[client_id].enable:
            if local_client_id.first() is None or len(local_client_id) < self.max_client:
                if client_id not in local_client_id:
                    # TODO: check
                    local_client_id[client_id] = client_queue(connection_id)
                else:
                    local_client_id[client_id].re_enable(connection_id)
                return local_client_id[client_id].insert(connection_id)

            if not local_client_id[local_client_id.first()].is_active():
                del local_client_id[local_client_id.first()]
                if client_id not in local_client_id:
                    # TODO: check
                    local_client_id[client_id] = client_queue(connection_id)
                else:
                    local_client_id[client_id].re_enable(connection_id)
                return local_client_id[client_id].insert(connection_id)

            logging.warn(self.name + ': no inactive client')
            return False
        else:
            return local_client_id[client_id].insert(connection_id)

    def remove(self, user_id, client_id):
        if user_id in self.user_id:
            local_client_id = self.user_id[user_id]
            if client_id in local_client_id:
                local_client_id[client_id].delref()


class auth_chain_a(auth_base):
    def __init__(self, method):
        super(auth_chain_a, self).__init__(method)
        self.hashfunc = hashlib.md5
        self.recv_buf = b''
        self.unit_len = 2800
        self.raw_trans = False
        self.has_sent_header = False
        self.has_recv_header = False
        self.client_id = 0
        self.connection_id = 0
        self.max_time_dif = 60 * 60 * 24  # time dif (second) setting
        self.salt = b"auth_chain_a"
        self.no_compatible_method = 'auth_chain_a'
        self.pack_id = 1
        self.recv_id = 1
        self.user_id = None
        self.user_id_num = 0
        self.user_key = None
        self.overhead = 4
        self.client_over_head = 4
        self.last_client_hash = b''
        self.last_server_hash = b''
        self.random_client = xorshift128plus()
        self.random_server = xorshift128plus()
        self.encryptor = None

    def init_data(self):
        return obfs_auth_chain_data(self.method)

    def get_overhead(self, direction):  # direction: true for c->s false for s->c
        return self.overhead

    def set_server_info(self, server_info):
        self.server_info = server_info
        try:
            max_client = int(server_info.protocol_param.split('#')[0])
        except:
            max_client = 64
        self.server_info.data.set_max_client(max_client)

    def trapezoid_random_float(self, d):
        if d == 0:
            return random.random()
        s = random.random()
        a = 1 - d
        return (math.sqrt(a * a + 4 * d * s) - a) / (2 * d)

    def trapezoid_random_int(self, max_val, d):
        v = self.trapezoid_random_float(d)
        return int(v * max_val)

    def rnd_data_len(self, buf_size, last_hash, random):
        if buf_size > 1440:
            return 0
        random.init_from_bin_len(last_hash, buf_size)
        if buf_size > 1300:
            return random.next() % 31
        if buf_size > 900:
            return random.next() % 127
        if buf_size > 400:
            return random.next() % 521
        return random.next() % 1021

    def udp_rnd_data_len(self, last_hash, random):
        random.init_from_bin(last_hash)
        return random.next() % 127

    def rnd_start_pos(self, rand_len, random):
        if rand_len > 0:
            return random.next() % 8589934609 % rand_len
        return 0

    def rnd_data(self, buf_size, buf, last_hash, random):
        rand_len = self.rnd_data_len(buf_size, last_hash, random)

        rnd_data_buf = rand_bytes(rand_len)

        if buf_size == 0:
            return rnd_data_buf
        else:
            if rand_len > 0:
                start_pos = self.rnd_start_pos(rand_len, random)
                return rnd_data_buf[:start_pos] + buf + rnd_data_buf[start_pos:]
            else:
                return buf

    def pack_client_data(self, buf):
        buf = self.encryptor.encrypt(buf)
        data = self.rnd_data(len(buf), buf, self.last_client_hash, self.random_client)
        mac_key = self.user_key + struct.pack('<I', self.pack_id)
        length = len(buf) ^ struct.unpack('<H', self.last_client_hash[14:])[0]
        data = struct.pack('<H', length) + data
        self.last_client_hash = hmac.new(mac_key, data, self.hashfunc).digest()
        data += self.last_client_hash[:2]
        self.pack_id = (self.pack_id + 1) & 0xFFFFFFFF
        return data

    def pack_server_data(self, buf):
        buf = self.encryptor.encrypt(buf)
        data = self.rnd_data(len(buf), buf, self.last_server_hash, self.random_server)
        mac_key = self.user_key + struct.pack('<I', self.pack_id)
        length = len(buf) ^ struct.unpack('<H', self.last_server_hash[14:])[0]
        data = struct.pack('<H', length) + data
        self.last_server_hash = hmac.new(mac_key, data, self.hashfunc).digest()
        data += self.last_server_hash[:2]
        self.pack_id = (self.pack_id + 1) & 0xFFFFFFFF
        return data

    def pack_auth_data(self, auth_data, buf):
        data = auth_data
        data = data + (struct.pack('<H', self.server_info.overhead) + struct.pack('<H', 0))
        mac_key = self.server_info.iv + self.server_info.key

        check_head = rand_bytes(4)
        self.last_client_hash = hmac.new(mac_key, check_head, self.hashfunc).digest()
        check_head += self.last_client_hash[:8]

        if b':' in to_bytes(self.server_info.protocol_param):
            try:
                items = to_bytes(self.server_info.protocol_param).split(b':')
                self.user_key = items[1]
                uid = struct.pack('<I', int(items[0]))
            except:
                uid = rand_bytes(4)
        else:
            uid = rand_bytes(4)
        if self.user_key is None:
            self.user_key = self.server_info.key

        encryptor = encrypt.Encryptor(
            to_bytes(base64.b64encode(self.user_key)) + self.salt, 'aes-128-cbc', b'\x00' * 16)

        uid = struct.unpack('<I', uid)[0] ^ struct.unpack('<I', self.last_client_hash[8:12])[0]
        uid = struct.pack('<I', uid)
        data = uid + encryptor.encrypt(data)[16:]
        self.last_server_hash = hmac.new(self.user_key, data, self.hashfunc).digest()
        data = check_head + data + self.last_server_hash[:4]
        self.encryptor = encrypt.Encryptor(
            to_bytes(base64.b64encode(self.user_key)) + to_bytes(base64.b64encode(self.last_client_hash)), 'rc4')
        return data + self.pack_client_data(buf)

    def auth_data(self):
        utc_time = int(time.time()) & 0xFFFFFFFF
        if self.server_info.data.connection_id > 0xFF000000:
            self.server_info.data.local_client_id = b''
        if not self.server_info.data.local_client_id:
            self.server_info.data.local_client_id = rand_bytes(4)
            logging.debug("local_client_id %s" % (binascii.hexlify(self.server_info.data.local_client_id),))
            self.server_info.data.connection_id = struct.unpack('<I', rand_bytes(4))[0] & 0xFFFFFF
        self.server_info.data.connection_id += 1
        return b''.join([struct.pack('<I', utc_time),
                         self.server_info.data.local_client_id,
                         struct.pack('<I', self.server_info.data.connection_id)])

    def on_recv_auth_data(self, utc_time):
        pass

    def client_pre_encrypt(self, buf):
        ret = b''
        ogn_data_len = len(buf)
        if not self.has_sent_header:
            head_size = self.get_head_size(buf, 30)
            datalen = min(len(buf), random.randint(0, 31) + head_size)
            ret += self.pack_auth_data(self.auth_data(), buf[:datalen])
            buf = buf[datalen:]
            self.has_sent_header = True
        while len(buf) > self.unit_len:
            ret += self.pack_client_data(buf[:self.unit_len])
            buf = buf[self.unit_len:]
        ret += self.pack_client_data(buf)
        return ret

    def client_post_decrypt(self, buf):
        if self.raw_trans:
            return buf
        self.recv_buf += buf
        out_buf = b''
        while len(self.recv_buf) > 4:
            mac_key = self.user_key + struct.pack('<I', self.recv_id)
            data_len = struct.unpack('<H', self.recv_buf[:2])[0] ^ struct.unpack('<H', self.last_server_hash[14:16])[0]
            rand_len = self.rnd_data_len(data_len, self.last_server_hash, self.random_server)
            length = data_len + rand_len
            if length >= 4096:
                self.raw_trans = True
                self.recv_buf = b''
                raise Exception('client_post_decrypt data error')

            if length + 4 > len(self.recv_buf):
                break

            server_hash = hmac.new(mac_key, self.recv_buf[:length + 2], self.hashfunc).digest()
            if server_hash[:2] != self.recv_buf[length + 2: length + 4]:
                logging.info('%s: checksum error, data %s'
                             % (self.no_compatible_method, binascii.hexlify(self.recv_buf[:length])))
                self.raw_trans = True
                self.recv_buf = b''
                raise Exception('client_post_decrypt data uncorrect checksum')

            pos = 2
            if data_len > 0 and rand_len > 0:
                pos = 2 + self.rnd_start_pos(rand_len, self.random_server)
            out_buf += self.encryptor.decrypt(self.recv_buf[pos: data_len + pos])
            self.last_server_hash = server_hash
            if self.recv_id == 1:
                self.server_info.tcp_mss = struct.unpack('<H', out_buf[:2])[0]
                out_buf = out_buf[2:]
            self.recv_id = (self.recv_id + 1) & 0xFFFFFFFF
            self.recv_buf = self.recv_buf[length + 4:]

        return out_buf

    def server_pre_encrypt(self, buf):
        if self.raw_trans:
            return buf
        ret = b''
        if self.pack_id == 1:
            tcp_mss = self.server_info.tcp_mss if self.server_info.tcp_mss < 1500 else 1500
            self.server_info.tcp_mss = tcp_mss
            buf = struct.pack('<H', tcp_mss) + buf
            self.unit_len = tcp_mss - self.client_over_head
        while len(buf) > self.unit_len:
            ret += self.pack_server_data(buf[:self.unit_len])
            buf = buf[self.unit_len:]
        ret += self.pack_server_data(buf)
        return ret

    def server_post_decrypt(self, buf):
        if self.raw_trans:
            return (buf, False)
        self.recv_buf += buf
        out_buf = b''
        sendback = False

        if not self.has_recv_header:
            if len(self.recv_buf) >= 12 or len(self.recv_buf) in [7, 8]:
                recv_len = min(len(self.recv_buf), 12)
                mac_key = self.server_info.recv_iv + self.server_info.key
                md5data = hmac.new(mac_key, self.recv_buf[:4], self.hashfunc).digest()
                if md5data[:recv_len - 4] != self.recv_buf[4:recv_len]:
                    return self.not_match_return(self.recv_buf)

            if len(self.recv_buf) < 12 + 24:
                return (b'', False)

            self.last_client_hash = md5data
            uid = struct.unpack('<I', self.recv_buf[12:16])[0] ^ struct.unpack('<I', md5data[8:12])[0]
            self.user_id_num = uid
            uid = struct.pack('<I', uid)
            if uid in self.server_info.users:
                self.user_id = uid
                self.user_key = self.server_info.users[uid]
                self.server_info.update_user_func(uid)
            else:
                self.user_id_num = 0
                if not self.server_info.users:
                    self.user_key = self.server_info.key
                else:
                    self.user_key = self.server_info.recv_iv

            md5data = hmac.new(self.user_key, self.recv_buf[12: 12 + 20], self.hashfunc).digest()
            if md5data[:4] != self.recv_buf[32:36]:
                logging.error('%s data uncorrect auth HMAC-MD5 from %s:%d, data %s' % (
                    self.no_compatible_method, self.server_info.client, self.server_info.client_port,
                    binascii.hexlify(self.recv_buf)
                ))
                if len(self.recv_buf) < 36:
                    return (b'', False)
                return self.not_match_return(self.recv_buf)

            self.last_server_hash = md5data
            encryptor = encrypt.Encryptor(to_bytes(base64.b64encode(self.user_key)) + self.salt, 'aes-128-cbc')
            head = encryptor.decrypt(b'\x00' * 16 + self.recv_buf[16:32] + b'\x00')  # need an extra byte or recv empty
            self.client_over_head = struct.unpack('<H', head[12:14])[0]

            utc_time = struct.unpack('<I', head[:4])[0]
            client_id = struct.unpack('<I', head[4:8])[0]
            connection_id = struct.unpack('<I', head[8:12])[0]
            time_dif = common.int32(utc_time - (int(time.time()) & 0xffffffff))
            if time_dif < -self.max_time_dif or time_dif > self.max_time_dif:
                logging.info('%s: wrong timestamp, time_dif %d, data %s' % (
                    self.no_compatible_method, time_dif, binascii.hexlify(head)
                ))
                return self.not_match_return(self.recv_buf)
            elif self.server_info.data.insert(self.user_id, client_id, connection_id):
                self.has_recv_header = True
                self.client_id = client_id
                self.connection_id = connection_id
            else:
                logging.info('%s: auth fail, data %s' % (self.no_compatible_method, binascii.hexlify(out_buf)))
                return self.not_match_return(self.recv_buf)

            self.on_recv_auth_data(utc_time)
            self.encryptor = encrypt.Encryptor(
                to_bytes(base64.b64encode(self.user_key)) + to_bytes(base64.b64encode(self.last_client_hash)), 'rc4')
            self.recv_buf = self.recv_buf[36:]
            self.has_recv_header = True
            sendback = True

        while len(self.recv_buf) > 4:
            mac_key = self.user_key + struct.pack('<I', self.recv_id)
            data_len = struct.unpack('<H', self.recv_buf[:2])[0] ^ struct.unpack('<H', self.last_client_hash[14:16])[0]
            rand_len = self.rnd_data_len(data_len, self.last_client_hash, self.random_client)
            length = data_len + rand_len
            if length >= 4096:
                self.raw_trans = True
                self.recv_buf = b''
                if self.recv_id == 1:
                    logging.info(self.no_compatible_method + ': over size')
                    return (b'E' * 2048, False)
                else:
                    raise Exception('server_post_decrype data error')

            if length + 4 > len(self.recv_buf):
                break

            client_hash = hmac.new(mac_key, self.recv_buf[:length + 2], self.hashfunc).digest()
            if client_hash[:2] != self.recv_buf[length + 2: length + 4]:
                logging.info('%s: checksum error, data %s' % (
                    self.no_compatible_method, binascii.hexlify(self.recv_buf[:length])
                ))
                self.raw_trans = True
                self.recv_buf = b''
                if self.recv_id == 1:
                    return (b'E' * 2048, False)
                else:
                    raise Exception('server_post_decrype data uncorrect checksum')

            self.recv_id = (self.recv_id + 1) & 0xFFFFFFFF
            pos = 2
            if data_len > 0 and rand_len > 0:
                pos = 2 + self.rnd_start_pos(rand_len, self.random_client)
            out_buf += self.encryptor.decrypt(self.recv_buf[pos: data_len + pos])
            self.last_client_hash = client_hash
            self.recv_buf = self.recv_buf[length + 4:]
            if data_len == 0:
                sendback = True

        if out_buf:
            self.server_info.data.update(self.user_id, self.client_id, self.connection_id)
        return (out_buf, sendback)

    def client_udp_pre_encrypt(self, buf):
        if self.user_key is None:
            if b':' in to_bytes(self.server_info.protocol_param):
                try:
                    items = to_bytes(self.server_info.protocol_param).split(':')
                    self.user_key = self.hashfunc(items[1]).digest()
                    self.user_id = struct.pack('<I', int(items[0]))
                except:
                    pass
            if self.user_key is None:
                self.user_id = rand_bytes(4)
                self.user_key = self.server_info.key
        authdata = rand_bytes(3)
        mac_key = self.server_info.key
        md5data = hmac.new(mac_key, authdata, self.hashfunc).digest()
        uid = struct.unpack('<I', self.user_id)[0] ^ struct.unpack('<I', md5data[:4])[0]
        uid = struct.pack('<I', uid)
        rand_len = self.udp_rnd_data_len(md5data, self.random_client)
        encryptor = encrypt.Encryptor(
            to_bytes(base64.b64encode(self.user_key)) + to_bytes(base64.b64encode(md5data)), 'rc4')
        out_buf = encryptor.encrypt(buf)
        buf = out_buf + rand_bytes(rand_len) + authdata + uid
        return buf + hmac.new(self.user_key, buf, self.hashfunc).digest()[:1]

    def client_udp_post_decrypt(self, buf):
        if len(buf) <= 8:
            return (b'', None)
        if hmac.new(self.user_key, buf[:-1], self.hashfunc).digest()[:1] != buf[-1:]:
            return (b'', None)
        mac_key = self.server_info.key
        md5data = hmac.new(mac_key, buf[-8:-1], self.hashfunc).digest()
        rand_len = self.udp_rnd_data_len(md5data, self.random_server)
        encryptor = encrypt.Encryptor(
            to_bytes(base64.b64encode(self.user_key)) + to_bytes(base64.b64encode(md5data)), 'rc4')
        return encryptor.decrypt(buf[:-8 - rand_len])

    def server_udp_pre_encrypt(self, buf, uid):
        if uid in self.server_info.users:
            user_key = self.server_info.users[uid]
        else:
            uid = None
            if not self.server_info.users:
                user_key = self.server_info.key
            else:
                user_key = self.server_info.recv_iv
        authdata = rand_bytes(7)
        mac_key = self.server_info.key
        md5data = hmac.new(mac_key, authdata, self.hashfunc).digest()
        rand_len = self.udp_rnd_data_len(md5data, self.random_server)
        encryptor = encrypt.Encryptor(to_bytes(base64.b64encode(user_key)) + to_bytes(base64.b64encode(md5data)), 'rc4')
        out_buf = encryptor.encrypt(buf)
        buf = out_buf + rand_bytes(rand_len) + authdata
        return buf + hmac.new(user_key, buf, self.hashfunc).digest()[:1]

    def server_udp_post_decrypt(self, buf):
        mac_key = self.server_info.key
        md5data = hmac.new(mac_key, buf[-8:-5], self.hashfunc).digest()
        uid = struct.unpack('<I', buf[-5:-1])[0] ^ struct.unpack('<I', md5data[:4])[0]
        uid = struct.pack('<I', uid)
        if uid in self.server_info.users:
            user_key = self.server_info.users[uid]
        else:
            uid = None
            if not self.server_info.users:
                user_key = self.server_info.key
            else:
                user_key = self.server_info.recv_iv
        if hmac.new(user_key, buf[:-1], self.hashfunc).digest()[:1] != buf[-1:]:
            return (b'', None)
        rand_len = self.udp_rnd_data_len(md5data, self.random_client)
        encryptor = encrypt.Encryptor(to_bytes(base64.b64encode(user_key)) + to_bytes(base64.b64encode(md5data)), 'rc4')
        out_buf = encryptor.decrypt(buf[:-8 - rand_len])
        return (out_buf, uid)

    def dispose(self):
        self.server_info.data.remove(self.user_id, self.client_id)


class auth_chain_b(auth_chain_a):
    def __init__(self, method):
        super(auth_chain_b, self).__init__(method)
        self.salt = b"auth_chain_b"
        self.no_compatible_method = 'auth_chain_b'
        # NOTE
        # 补全后长度数组
        # 随机在其中选择一个补全到的长度
        # 为每个连接初始化一个固定内容的数组
        self.data_size_list = []
        self.data_size_list2 = []

    def init_data_size(self, key):
        if self.data_size_list:
            self.data_size_list = []
            self.data_size_list2 = []
        random = xorshift128plus()
        random.init_from_bin(key)
        # 补全数组长为4~12-1
        list_len = random.next() % 8 + 4
        for i in range(0, list_len):
            self.data_size_list.append((int)(random.next() % 2340 % 2040 % 1440))
        self.data_size_list.sort()
        # 补全数组长为8~24-1
        list_len = random.next() % 16 + 8
        for i in range(0, list_len):
            self.data_size_list2.append((int)(random.next() % 2340 % 2040 % 1440))
        self.data_size_list2.sort()

    def set_server_info(self, server_info):
        self.server_info = server_info
        try:
            max_client = int(server_info.protocol_param.split('#')[0])
        except:
            max_client = 64
        self.server_info.data.set_max_client(max_client)
        self.init_data_size(self.server_info.key)

    def rnd_data_len(self, buf_size, last_hash, random):
        if buf_size >= 1440:
            return 0
        random.init_from_bin_len(last_hash, buf_size)
        pos = bisect.bisect_left(self.data_size_list, buf_size + self.server_info.overhead)
        final_pos = pos + random.next() % (len(self.data_size_list))
        # 假设random均匀分布，则越长的原始数据长度越容易if false
        if final_pos < len(self.data_size_list):
            return self.data_size_list[final_pos] - buf_size - self.server_info.overhead

        # 上面if false后选择2号补全数组，此处有更精细的长度分段
        pos = bisect.bisect_left(self.data_size_list2, buf_size + self.server_info.overhead)
        final_pos = pos + random.next() % (len(self.data_size_list2))
        if final_pos < len(self.data_size_list2):
            return self.data_size_list2[final_pos] - buf_size - self.server_info.overhead
        # final_pos 总是分布在pos~(data_size_list2.len-1)之间
        if final_pos < pos + len(self.data_size_list2) - 1:
            return 0
        # 有1/len(self.data_size_list2)的概率不满足上一个if

        if buf_size > 1300:
            return random.next() % 31
        if buf_size > 900:
            return random.next() % 127
        if buf_size > 400:
            return random.next() % 521
        return random.next() % 1021


class auth_chain_c(auth_chain_b):
    def __init__(self, method):
        super(auth_chain_c, self).__init__(method)
        self.salt = b"auth_chain_c"
        self.no_compatible_method = 'auth_chain_c'
        self.data_size_list0 = []

    def init_data_size(self, key):
        if self.data_size_list0:
            self.data_size_list0 = []
        random = xorshift128plus()
        random.init_from_bin(key)
        # 补全数组长为12~24-1
        list_len = random.next() % (8 + 16) + (4 + 8)
        for i in range(0, list_len):
            self.data_size_list0.append((int)(random.next() % 2340 % 2040 % 1440))
        self.data_size_list0.sort()

    def set_server_info(self, server_info):
        self.server_info = server_info
        try:
            max_client = int(server_info.protocol_param.split('#')[0])
        except:
            max_client = 64
        self.server_info.data.set_max_client(max_client)
        self.init_data_size(self.server_info.key)

    def rnd_data_len(self, buf_size, last_hash, random):
        other_data_size = buf_size + self.server_info.overhead
        # 一定要在random使用前初始化，以保证服务器与客户端同步，保证包大小验证结果正确
        random.init_from_bin_len(last_hash, buf_size)
        # final_pos 总是分布在pos~(data_size_list0.len-1)之间
        # 除非data_size_list0中的任何值均过小使其全部都无法容纳buf
        if other_data_size >= self.data_size_list0[-1]:
            if other_data_size >= 1440:
                return 0
            if other_data_size > 1300:
                return random.next() % 31
            if other_data_size > 900:
                return random.next() % 127
            if other_data_size > 400:
                return random.next() % 521
            return random.next() % 1021

        pos = bisect.bisect_left(self.data_size_list0, other_data_size)
        # random select a size in the leftover data_size_list0
        final_pos = pos + random.next() % (len(self.data_size_list0) - pos)
        return self.data_size_list0[final_pos] - other_data_size


class auth_chain_d(auth_chain_b):
    def __init__(self, method):
        super(auth_chain_d, self).__init__(method)
        self.salt = b"auth_chain_d"
        self.no_compatible_method = 'auth_chain_d'
        self.data_size_list0 = []

    def check_and_patch_data_size(self, random):
        # append new item
        # when the biggest item(first time) or the last append item(other time) are not big enough.
        # but set a limit size (64) to avoid stack overflow.
        if self.data_size_list0[-1] < 1300 and len(self.data_size_list0) < 64:
            self.data_size_list0.append((int)(random.next() % 2340 % 2040 % 1440))
            self.check_and_patch_data_size(random)

    def init_data_size(self, key):
        if self.data_size_list0:
            self.data_size_list0 = []
        random = xorshift128plus()
        random.init_from_bin(key)
        # 补全数组长为12~24-1
        list_len = random.next() % (8 + 16) + (4 + 8)
        for i in range(0, list_len):
            self.data_size_list0.append((int)(random.next() % 2340 % 2040 % 1440))
        self.data_size_list0.sort()
        old_len = len(self.data_size_list0)
        self.check_and_patch_data_size(random)
        # if check_and_patch_data_size are work, re-sort again.
        if old_len != len(self.data_size_list0):
            self.data_size_list0.sort()

    def set_server_info(self, server_info):
        self.server_info = server_info
        try:
            max_client = int(server_info.protocol_param.split('#')[0])
        except:
            max_client = 64
        self.server_info.data.set_max_client(max_client)
        self.init_data_size(self.server_info.key)

    def rnd_data_len(self, buf_size, last_hash, random):
        other_data_size = buf_size + self.server_info.overhead
        # if other_data_size > the bigest item in data_size_list0, not padding any data
        if other_data_size >= self.data_size_list0[-1]:
            return 0

        random.init_from_bin_len(last_hash, buf_size)
        pos = bisect.bisect_left(self.data_size_list0, other_data_size)
        # random select a size in the leftover data_size_list0
        final_pos = pos + random.next() % (len(self.data_size_list0) - pos)
        return self.data_size_list0[final_pos] - other_data_size


class auth_chain_e(auth_chain_d):
    def __init__(self, method):
        super(auth_chain_e, self).__init__(method)
        self.salt = b"auth_chain_e"
        self.no_compatible_method = 'auth_chain_e'

    def rnd_data_len(self, buf_size, last_hash, random):
        random.init_from_bin_len(last_hash, buf_size)
        other_data_size = buf_size + self.server_info.overhead
        # if other_data_size > the bigest item in data_size_list0, not padding any data
        if other_data_size >= self.data_size_list0[-1]:
            return 0

        # use the mini size in the data_size_list0
        pos = bisect.bisect_left(self.data_size_list0, other_data_size)
        return self.data_size_list0[pos] - other_data_size


# auth_chain_f
# when every connect create, generate size_list will different when every day or every custom time interval which set in the config
class auth_chain_f(auth_chain_e):
    def __init__(self, method):
        super(auth_chain_f, self).__init__(method)
        self.salt = b"auth_chain_f"
        self.no_compatible_method = 'auth_chain_f'

    def set_server_info(self, server_info):
        self.server_info = server_info
        try:
            max_client = int(server_info.protocol_param.split('#')[0])
        except:
            max_client = 64
        self.server_info.data.set_max_client(max_client)
        try:
            self.key_change_interval = int(server_info.protocol_param.split('#')[1])  # config are in second
        except:
            self.key_change_interval = 60 * 60 * 24  # a day by second
        self.key_change_datetime_key = int(int(time.time()) / self.key_change_interval)
        self.key_change_datetime_key_bytes = []  # big bit first list
        for i in range(7, -1, -1):  # big-ending compare to c
            self.key_change_datetime_key_bytes.append((self.key_change_datetime_key >> (8 * i)) & 0xFF)
        self.init_data_size(self.server_info.key)

    def init_data_size(self, key):
        if self.data_size_list0:
            self.data_size_list0 = []
        random = xorshift128plus()
        # key xor with key_change_datetime_key
        new_key = bytearray(key)
        for i in range(0, 8):
            new_key[i] ^= self.key_change_datetime_key_bytes[i]
        random.init_from_bin(new_key)
        # 补全数组长为12~24-1
        list_len = random.next() % (8 + 16) + (4 + 8)
        for i in range(0, list_len):
            self.data_size_list0.append(int(random.next() % 2340 % 2040 % 1440))
        self.data_size_list0.sort()
        old_len = len(self.data_size_list0)
        self.check_and_patch_data_size(random)
        # if check_and_patch_data_size are work, re-sort again.
        if old_len != len(self.data_size_list0):
            self.data_size_list0.sort()
