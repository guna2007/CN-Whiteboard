import argparse
import re
import secrets
import socket
import threading
from collections import deque
from datetime import datetime, timezone
from queue import Empty, Full, Queue

from protocol import (
    BOARD_HEIGHT, BOARD_WIDTH, HEARTBEAT, MAX_ITEMS, MAX_POINTS, PALETTE,
    PORT, ROOM_POINT_LIMIT, encode, receive,
)


KINDS = {'pen', 'line', 'arrow', 'rectangle', 'note'}
EDIT_COMMANDS = {'start', 'points', 'cancel', 'finish', 'add', 'erase', 'undo', 'redo'}
HOST_COMMANDS = {'clear', 'save', 'restore'}
NUMBERED = {'finish', 'add', 'erase', 'undo', 'redo', 'clear', 'restore', 'chat'}
CODE_CHARS = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'


def point(value):
    if (not isinstance(value, list) or len(value) != 2
            or any(type(n) is not int for n in value)
            or not 0 <= value[0] <= BOARD_WIDTH or not 0 <= value[1] <= BOARD_HEIGHT):
        raise ValueError('Invalid canvas coordinates.')
    return value


def short_text(value, limit):
    if not isinstance(value, str):
        raise ValueError('Expected text.')
    value = value.strip()
    if not 1 <= len(value) <= limit or any(not c.isprintable() for c in value):
        raise ValueError(f'Enter 1 to {limit} printable characters.')
    return value


def board_item(raw, author):
    if not isinstance(raw, dict):
        raise ValueError('Invalid canvas item.')
    kind, color, width, points = (raw.get(key) for key in ('kind', 'color', 'width', 'points'))
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError('Unknown drawing tool.')
    if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
        raise ValueError('Invalid color.')
    if type(width) is not int or not 1 <= width <= 18:
        raise ValueError('Invalid line width.')
    low, high = (1, MAX_POINTS) if kind == 'pen' else (1, 1) if kind == 'note' else (2, 2)
    if not isinstance(points, list) or not low <= len(points) <= high:
        raise ValueError('Invalid number of points.')
    item = {'kind': kind, 'color': color.lower(), 'width': width,
            'points': [point(p) for p in points], 'author': author}
    if kind == 'note':
        item['text'] = short_text(raw.get('text'), 140)
    return item


class Participant:
    def __init__(self, name, role, index):
        self.name = name
        self.role = role
        self.id = secrets.token_hex(6)
        self.token = secrets.token_urlsafe(24)
        self.color = PALETTE[index % len(PALETTE)]
        self.last_op = 0
        self.undo = []
        self.redo = []

    def public(self):
        return {'id': self.id, 'name': self.name, 'role': self.role, 'color': self.color}


class Peer:
    def __init__(self, sock):
        self.sock = sock
        self.room = None
        self.user = None
        self.outbox = Queue(maxsize=128)
        self.closed = threading.Event()

    def close(self):
        self.closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def queue(self, frame):
        if not self.closed.is_set():
            try:
                self.outbox.put_nowait(frame)
            except Full:
                self.close()

    def send(self, message):
        self.queue(encode(message))

    def writer(self):
        try:
            while not self.closed.is_set():
                try:
                    frame = self.outbox.get(timeout=HEARTBEAT)
                except Empty:
                    frame = encode({'type': 'heartbeat'})
                self.sock.sendall(frame)
        except OSError:
            self.close()


class Room:
    def __init__(self, code, view_code, title):
        self.code = code
        self.view_code = view_code
        self.title = title
        self.lock = threading.Lock()
        self.members = {}
        self.peers = {}
        self.items = {}
        self.enabled = set()
        self.hidden = {}
        self.drafts = {}
        self.chat = deque(maxlen=100)
        self.revision = 0
        self.next_item = 1
        self.next_action = 1
        self.point_count = 0

    def broadcast(self, message, skip=None):
        frame = encode(message)
        for peer in self.peers.values():
            if peer is not skip:
                peer.queue(frame)

    def visible(self, item_id):
        return item_id in self.enabled and not self.hidden.get(item_id)

    def board(self):
        return [item for item_id, item in self.items.items() if self.visible(item_id)]

    def snapshot(self):
        return {'type': 'snapshot', 'rev': self.revision, 'items': self.board(),
                'drafts': list(self.drafts.values()), 'chat': list(self.chat),
                'participants': self.roster()}

    def roster(self):
        return [peer.user.public() for peer in self.peers.values()]

    def join(self, peer, name, role, token=None):
        with self.lock:
            if token:
                user = self.members.get(token)
                if user is None:
                    raise ValueError('This session can no longer be resumed.')
                old_peer = self.peers.get(user.id)
            else:
                if len(self.members) >= 100:
                    raise ValueError('This room has reached its participant limit.')
                user = Participant(name, role, len(self.members))
                old_peer = None
            if len(self.peers) >= 16 and old_peer is None:
                raise ValueError('This room already has 16 connected participants.')
            if any(other is not old_peer and other.user.name.casefold() == user.name.casefold()
                   for other in self.peers.values()):
                raise ValueError('That username is already connected.')
            if old_peer is not None:
                old_peer.close()
                self.cancel_draft(user.id)
            self.members[user.token] = user
            self.peers[user.id] = peer
            peer.room, peer.user = self, user
            welcome = {'type': 'welcome', 'title': self.title, 'user': user.public(),
                       'token': user.token, 'last_op': user.last_op}
            if user.role == 'host':
                welcome.update(code=self.code, view_code=self.view_code)
            peer.send(welcome)
            peer.send(self.snapshot())
            self.broadcast({'type': 'presence', 'participants': self.roster(),
                            'notice': f'{user.name} {"rejoined" if token else "joined"}.'})

    def leave(self, peer):
        with self.lock:
            user = peer.user
            if self.peers.get(user.id) is not peer:
                return
            del self.peers[user.id]
            self.cancel_draft(user.id)
            self.broadcast({'type': 'presence', 'participants': self.roster(),
                            'notice': f'{user.name} left.'})

    def cancel_draft(self, user_id):
        if self.drafts.pop(user_id, None) is not None:
            self.broadcast({'type': 'draft_end', 'user': user_id})

    def check_space(self, points=0, items=0):
        active_points = sum(len(draft['points']) for draft in self.drafts.values())
        if self.point_count + active_points + points > ROOM_POINT_LIMIT:
            raise ValueError('Board is full. Ask the host to save and clear it.')
        if len(self.items) + len(self.drafts) + items > MAX_ITEMS:
            raise ValueError('Board is full. Ask the host to save and clear it.')

    def add(self, item):
        item_id = self.next_item
        self.next_item += 1
        self.items[item_id] = {**item, 'id': item_id}
        self.point_count += len(item['points'])
        return item_id

    def change(self, action, enabled):
        action_id, kind, item_id = action
        if kind == 'add':
            if enabled:
                self.enabled.add(item_id)
            else:
                self.enabled.discard(item_id)
        else:
            marks = self.hidden.setdefault(item_id, set())
            if enabled:
                marks.add(action_id)
            else:
                marks.discard(action_id)
        self.revision += 1
        message = {'type': 'change', 'rev': self.revision, 'id': item_id,
                   'item': self.items[item_id] if self.visible(item_id) else None}
        self.broadcast(message)

    def record(self, user, kind, item_id):
        action = (self.next_action, kind, item_id)
        self.next_action += 1
        user.undo.append(action)
        user.redo.clear()
        self.change(action, True)

    def replace(self, raw_items):
        if not isinstance(raw_items, list) or len(raw_items) > MAX_ITEMS:
            raise ValueError('Invalid board file.')
        items = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise ValueError('Invalid canvas item.')
            items.append(board_item(raw, short_text(raw.get('author'), 24)))
        if sum(len(item['points']) for item in items) > ROOM_POINT_LIMIT:
            raise ValueError('Board file is too large.')
        self.items.clear()
        self.enabled.clear()
        self.hidden.clear()
        self.drafts.clear()
        self.point_count = 0
        for member in self.members.values():
            member.undo.clear()
            member.redo.clear()
        for item in items:
            self.enabled.add(self.add(item))
        self.revision += 1
        self.broadcast(self.snapshot())

    def handle(self, peer, message):
        with self.lock:
            user = peer.user
            if self.peers.get(user.id) is not peer:
                return
            kind = message['type']
            if kind in EDIT_COMMANDS and user.role == 'viewer':
                raise ValueError('Viewers cannot edit the board.')
            if kind in HOST_COMMANDS and user.role != 'host':
                raise ValueError('Only the host can do that.')
            if kind in NUMBERED:
                op = message.get('op')
                if type(op) is not int or not 0 < op < 2**53:
                    raise ValueError('Invalid operation number.')
                if op <= user.last_op:
                    return
                user.last_op = op
            self.dispatch(peer, kind, message)

    def dispatch(self, peer, kind, message):
        user = peer.user
        if kind == 'heartbeat':
            return
        if kind == 'sync':
            peer.send(self.snapshot())
        elif kind == 'cursor':
            self.broadcast({'type': 'cursor', 'user': user.id, 'point': point(message.get('point'))}, peer)
        elif kind == 'cursor_end':
            self.broadcast({'type': 'cursor_end', 'user': user.id}, peer)
        elif kind == 'start':
            item = board_item(message.get('item'), user.name)
            if item['kind'] != 'pen' or len(item['points']) != 1:
                raise ValueError('A stroke starts with one point.')
            self.cancel_draft(user.id)
            self.check_space(1, 1)
            self.drafts[user.id] = {**item, 'user': user.id}
            self.broadcast({'type': 'draft', 'item': self.drafts[user.id]})
        elif kind == 'points':
            draft = self.drafts.get(user.id)
            points = message.get('points')
            if draft is None:
                return
            if not isinstance(points, list) or not 1 <= len(points) <= 64:
                raise ValueError('Invalid stroke batch.')
            points = [point(p) for p in points]
            try:
                self.check_space(len(points))
                if len(draft['points']) + len(points) > MAX_POINTS:
                    raise ValueError('Stroke is too long. Start a new one.')
            except ValueError:
                self.cancel_draft(user.id)
                raise
            draft['points'].extend(points)
            self.broadcast({'type': 'draft_points', 'user': user.id, 'points': points})
        elif kind == 'cancel':
            self.cancel_draft(user.id)
        elif kind == 'finish':
            draft = self.drafts.get(user.id)
            if draft:
                self.cancel_draft(user.id)
                item = {key: value for key, value in draft.items() if key != 'user'}
                self.record(user, 'add', self.add(item))
        elif kind == 'add':
            item = board_item(message.get('item'), user.name)
            self.check_space(len(item['points']), 1)
            self.record(user, 'add', self.add(item))
        elif kind == 'erase':
            item_id = message.get('id')
            if type(item_id) is int and self.visible(item_id):
                self.record(user, 'erase', item_id)
        elif kind in ('undo', 'redo'):
            source, destination = (user.undo, user.redo) if kind == 'undo' else (user.redo, user.undo)
            if not source:
                raise ValueError(f'Nothing to {kind}.')
            action = source.pop()
            destination.append(action)
            self.change(action, kind == 'redo')
        elif kind == 'clear':
            self.replace([])
            self.notice(f'{user.name} cleared the board.')
        elif kind == 'save':
            peer.send({'type': 'saved', 'board': {'version': 1, 'items': self.board()}})
        elif kind == 'restore':
            board = message.get('board')
            if not isinstance(board, dict) or type(board.get('version')) is not int or board['version'] != 1:
                raise ValueError('Unsupported board file version.')
            self.replace(board.get('items'))
            self.notice(f'{user.name} restored the board.')
        elif kind == 'chat':
            entry = {'name': user.name, 'text': short_text(message.get('text'), 400),
                     'time': datetime.now(timezone.utc).isoformat(timespec='seconds')}
            self.chat.append(entry)
            self.broadcast({'type': 'chat', 'entry': entry})
        else:
            raise ValueError('Unknown message type.')

    def notice(self, text):
        self.broadcast({'type': 'presence', 'participants': self.roster(), 'notice': text})


class Server:
    def __init__(self):
        self.rooms = {}
        self.lock = threading.Lock()

    def code(self):
        while True:
            code = ''.join(secrets.choice(CODE_CHARS) for _ in range(8))
            if code not in self.rooms:
                return code

    def enter(self, peer, message):
        kind = message['type']
        name = short_text(message.get('name'), 24)
        if kind == 'create':
            title = short_text(message.get('title'), 40)
            with self.lock:
                if len(self.rooms) >= 48:
                    raise ValueError('The server has reached its room limit.')
                editor_code = self.code()
                viewer_code = self.code()
                while viewer_code == editor_code:
                    viewer_code = self.code()
                room = Room(editor_code, viewer_code, title)
                self.rooms[editor_code] = (room, 'editor')
                self.rooms[viewer_code] = (room, 'viewer')
            room.join(peer, name, 'host')
        elif kind == 'join':
            code = short_text(message.get('code'), 8).upper()
            with self.lock:
                result = self.rooms.get(code)
            if result is None:
                raise ValueError('Room code not found.')
            room, role = result
            token = message.get('token')
            if token is not None:
                token = short_text(token, 64)
            room.join(peer, name, role, token)
        else:
            raise ValueError('Create or join a room first.')

    def serve_peer(self, sock):
        peer = Peer(sock)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(1.0)
            try:
                self.enter(peer, receive(sock))
            except ValueError as exc:
                sock.sendall(encode({'type': 'error', 'text': str(exc)}))
                return
            threading.Thread(target=peer.writer, daemon=True).start()
            while not peer.closed.is_set():
                message = receive(sock)
                try:
                    peer.room.handle(peer, message)
                except ValueError as exc:
                    peer.send({'type': 'error', 'text': str(exc)})
        except (OSError, EOFError, ValueError, RecursionError):
            pass
        finally:
            peer.close()
            if peer.room is not None:
                peer.room.leave(peer)
            sock.close()


def main():
    parser = argparse.ArgumentParser(description='Host a LAN whiteboard server.')
    parser.add_argument('--bind', default='0.0.0.0', help='IPv4 address to listen on')
    parser.add_argument('--port', type=int, default=PORT)
    args = parser.parse_args()
    server = Server()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((args.bind, args.port))
            listener.listen()
            print(f'Whiteboard listening on {args.bind}:{listener.getsockname()[1]}', flush=True)
            while True:
                sock, _ = listener.accept()
                threading.Thread(target=server.serve_peer, args=(sock,), daemon=True).start()
        except KeyboardInterrupt:
            print('\nServer stopped.')
        except OSError as exc:
            parser.exit(1, f'Could not run server: {exc}\n')


if __name__ == '__main__':
    main()
