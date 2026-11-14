import json
import struct
import time


PORT = 5050
BOARD_WIDTH = 960
BOARD_HEIGHT = 600
MAX_FRAME = 4 * 1024 * 1024
HEARTBEAT = 4
TIMEOUT = 14
MAX_ITEMS = 600
MAX_POINTS = 4096
ROOM_POINT_LIMIT = 75_000
PALETTE = ('#234b82', '#b44d30', '#168072', '#8047a0', '#9c6817', '#425570')
HEADER = struct.Struct('!I')


def encode(message):
    body = json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if not 0 < len(body) <= MAX_FRAME:
        raise ValueError('Message is too large.')
    return HEADER.pack(len(body)) + body


def read_exact(sock, size, deadline):
    data = bytearray()
    while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Peer stopped responding.')
        try:
            chunk = sock.recv(min(size - len(data), 65536))
        except TimeoutError:
            continue
        if not chunk:
            raise EOFError('Peer disconnected.')
        data.extend(chunk)
    return data


def receive(sock):
    deadline = time.monotonic() + TIMEOUT
    size, = HEADER.unpack(read_exact(sock, HEADER.size, deadline))
    if not 0 < size <= MAX_FRAME:
        raise ValueError('Invalid frame size.')
    message = json.loads(read_exact(sock, size, deadline).decode('utf-8'))
    if not isinstance(message, dict) or not isinstance(message.get('type'), str):
        raise ValueError('A message must have a type.')
    return message
