import json
import socket
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from queue import Empty, Full, Queue
from tempfile import NamedTemporaryFile
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk

from protocol import BOARD_HEIGHT, BOARD_WIDTH, HEARTBEAT, MAX_FRAME, PORT, encode, receive


INK = '#172a3a'
MUTED = '#496174'
PANEL = '#edf2f5'
PAPER = '#ffffff'
ACCENT = '#12645c'
COLORS = ('#172a3a', '#2464aa', '#167e71', '#b74435', '#8557a4', '#c28b22')
TOOLS = ('Pen', 'Eraser', 'Line', 'Arrow', 'Rectangle', 'Note')
FILE_TYPES = [('Whiteboard file', '*.json')]


class Connection:
    def __init__(self, address, hello):
        self.address = address
        self.hello = hello
        self.inbox = Queue(maxsize=512)
        self.outbox = Queue(maxsize=128)
        self.closed = threading.Event()
        self.sock_lock = threading.Lock()
        self.sock = None

    def start(self):
        threading.Thread(target=self.reader, daemon=True).start()

    def send(self, message):
        try:
            if self.closed.is_set():
                return False
            self.outbox.put_nowait(encode(message))
            return True
        except ValueError as exc:
            self.deliver({'type': 'error', 'text': str(exc)})
            return False
        except Full:
            self.stop()
            return False

    def deliver(self, message):
        try:
            self.inbox.put_nowait(message)
        except Full:
            self.stop()

    def stop(self):
        self.closed.set()
        with self.sock_lock:
            if self.sock is not None:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def reader(self):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(4)
                sock.connect(self.address)
                sock.settimeout(1)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with self.sock_lock:
                    if self.closed.is_set():
                        return
                    self.sock = sock
                sock.sendall(encode(self.hello))
                threading.Thread(target=self.writer, args=(sock,), daemon=True).start()
                while not self.closed.is_set():
                    message = receive(sock)
                    if message['type'] != 'heartbeat':
                        self.deliver(message)
        except (OSError, EOFError, ValueError, RecursionError) as exc:
            if not self.closed.is_set():
                self.deliver({'type': 'error', 'text': str(exc)})
        finally:
            self.stop()

    def writer(self, sock):
        try:
            while not self.closed.is_set():
                try:
                    frame = self.outbox.get(timeout=HEARTBEAT)
                except Empty:
                    frame = encode({'type': 'heartbeat'})
                if not self.closed.is_set():
                    sock.sendall(frame)
        except OSError:
            self.stop()


class Whiteboard:
    def __init__(self, root):
        self.root = root
        root.title('LAN Whiteboard')
        root.geometry('1200x760')
        root.minsize(880, 600)
        root.configure(bg=PAPER)
        self.connection = None
        self.address = None
        self.hello = None
        self.user = None
        self.token = None
        self.code = ''
        self.view_code = ''
        self.op = 0
        self.ready = False
        self.syncing = False
        self.retry_at = False
        self.retry_delay = 1000
        self.save_path = None
        self.revision = 0
        self.items = {}
        self.drafts = {}
        self.people = {}
        self.cursors = {}
        self.drag = None
        self.pending_points = []
        self.cursor_point = None
        self.sent_cursor = None
        self.scale = 1
        self.offset_x = self.offset_y = 0
        self.color = COLORS[0]
        self.tool = tk.StringVar(value='Pen')
        self.width = tk.StringVar(value='4')
        self.status = tk.StringVar(value='Enter a LAN address to begin.')
        self.room_name = tk.StringVar()
        self.identity = tk.StringVar()
        self.invite_text = tk.StringVar()
        self.connection_label = tk.StringVar()
        self.configure_styles()
        self.make_lobby()
        self.make_workspace()
        self.lobby.pack(fill='both', expand=True)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(20, self.poll)
        root.after(33, self.flush_pointer)

    def configure_styles(self):
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.', font=('TkDefaultFont', 10), foreground=INK, background=PAPER)
        style.configure('TButton', padding=(9, 5), background=PANEL)
        style.map('TButton', background=[('active', '#dae7eb')])
        style.configure('Accent.TButton', background=ACCENT, foreground=PAPER)
        style.map('Accent.TButton', background=[('active', '#0c524d')])
        style.configure('Tool.TRadiobutton', padding=(7, 5), background=PANEL)
        style.map('Tool.TRadiobutton', background=[('selected', '#cde7e1'), ('active', '#dbe8ed')])

    def make_lobby(self):
        self.lobby = ttk.Frame(self.root, padding=30)
        form = ttk.Frame(self.lobby, padding=(32, 25))
        form.place(relx=.5, rely=.47, anchor='center')
        ttk.Label(form, text='LAN Whiteboard', font=('TkDefaultFont', 30, 'bold')).grid(
            row=0, column=0, columnspan=2, sticky='w', pady=(0, 5))
        ttk.Label(form, text='Draw and discuss together on the local network.', foreground=MUTED).grid(
            row=1, column=0, columnspan=2, sticky='w', pady=(0, 24))
        self.fields = {}
        for row, (key, label, default) in enumerate((
            ('address', 'Server address', '127.0.0.1'), ('port', 'Port', str(PORT)),
            ('name', 'Username', ''), ('title', 'New room name', ''), ('code', 'Join code', ''),
        ), start=2):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky='w', padx=(0, 15), pady=5)
            entry = ttk.Entry(form, width=32)
            entry.insert(0, default)
            entry.grid(row=row, column=1, sticky='ew', pady=5)
            self.fields[key] = entry
        buttons = ttk.Frame(form)
        buttons.grid(row=7, column=0, columnspan=2, sticky='w', pady=(16, 12))
        ttk.Button(buttons, text='Create room', style='Accent.TButton', command=lambda: self.enter('create')).pack(side='left')
        ttk.Button(buttons, text='Join room', command=lambda: self.enter('join')).pack(side='left', padx=8)
        ttk.Label(form, textvariable=self.status, wraplength=390, foreground=MUTED).grid(
            row=8, column=0, columnspan=2, sticky='w')

    def make_workspace(self):
        self.workspace = ttk.Frame(self.root, padding=(18, 14))
        top = ttk.Frame(self.workspace)
        top.pack(fill='x', pady=(0, 12))
        ttk.Label(top, textvariable=self.room_name, font=('TkDefaultFont', 22, 'bold')).pack(anchor='w')
        ttk.Label(top, textvariable=self.identity, foreground=MUTED).pack(anchor='w', pady=(2, 0))
        controls = ttk.Frame(self.workspace)
        controls.pack(fill='x', pady=(0, 10))
        self.edit_widgets = []
        for name in TOOLS:
            button = ttk.Radiobutton(controls, text=name, value=name, variable=self.tool, style='Tool.TRadiobutton')
            button.pack(side='left', padx=(0, 3))
            self.edit_widgets.append(button)
        ttk.Label(controls, text='Width').pack(side='left', padx=(12, 4))
        self.widths = ttk.Combobox(controls, values=('2', '4', '7', '12', '18'), width=3, state='readonly', textvariable=self.width)
        self.widths.pack(side='left')
        self.swatches = []
        for color in COLORS:
            swatch = tk.Button(controls, width=2, bg=color, activebackground=color, bd=1,
                               command=lambda value=color: self.select_color(value))
            swatch.pack(side='left', padx=(8 if color == COLORS[0] else 3, 0))
            self.swatches.append(swatch)
            self.edit_widgets.append(swatch)
        color_button = ttk.Button(controls, text='Color…', command=self.pick_color)
        color_button.pack(side='left', padx=(7, 0))
        self.edit_widgets.append(color_button)
        self.select_color(self.color)
        for name in ('Undo', 'Redo'):
            button = ttk.Button(controls, text=name, command=lambda value=name: self.send(value.lower(), numbered=True))
            button.pack(side='left', padx=(7, 0))
            self.edit_widgets.append(button)

        actions = ttk.Frame(self.workspace)
        actions.pack(fill='x', pady=(0, 9))
        self.host_controls = ttk.Frame(actions)
        self.host_controls.pack(side='left')
        self.host_buttons = []
        for label, command in (('Save board', self.save), ('Restore board', self.restore), ('Clear board', self.clear)):
            button = ttk.Button(self.host_controls, text=label, command=command)
            button.pack(side='left', padx=(0, 6))
            self.host_buttons.append(button)
        self.copy_button = ttk.Button(self.host_controls, text='Copy editor code', command=self.copy_code)
        self.copy_button.pack(side='left', padx=(0, 6))
        ttk.Label(actions, textvariable=self.invite_text, foreground=MUTED).pack(side='left')
        ttk.Button(actions, text='Leave room', command=self.leave).pack(side='right')

        main = ttk.Frame(self.workspace)
        main.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(main, bg='#e6edf1', highlightthickness=0, cursor='crosshair')
        self.canvas.pack(side='left', fill='both', expand=True, padx=(0, 14))
        side = ttk.Frame(main, width=252)
        side.pack(side='right', fill='y')
        side.pack_propagate(False)
        self.roster_heading = ttk.Label(side, text='Participants', font=('TkDefaultFont', 12, 'bold'))
        self.roster_heading.pack(anchor='w')
        roster_pane = ttk.Frame(side)
        roster_pane.pack(fill='x', pady=(6, 14))
        scroll = ttk.Scrollbar(roster_pane)
        self.roster = tk.Listbox(roster_pane, height=4, relief='flat', highlightthickness=0, bg=PANEL,
                                fg=INK, activestyle='none', yscrollcommand=scroll.set)
        scroll.configure(command=self.roster.yview)
        scroll.pack(side='right', fill='y')
        self.roster.pack(side='left', fill='both', expand=True)
        ttk.Label(side, text='Room chat', font=('TkDefaultFont', 12, 'bold')).pack(anchor='w', pady=(0, 6))
        chat_pane = ttk.Frame(side)
        chat_pane.pack(fill='both', expand=True)
        scroll = ttk.Scrollbar(chat_pane)
        self.chat = tk.Text(chat_pane, width=20, height=5, bg=PANEL, fg=INK, relief='flat', wrap='word',
                            state='disabled', padx=9, pady=8, yscrollcommand=scroll.set)
        self.chat.tag_configure('meta', foreground=ACCENT, spacing1=6)
        self.chat.tag_configure('notice', foreground=MUTED, spacing1=6)
        scroll.configure(command=self.chat.yview)
        scroll.pack(side='right', fill='y')
        self.chat.pack(side='left', fill='both', expand=True)
        composer = ttk.Frame(side)
        composer.pack(fill='x', pady=(8, 0))
        self.chat_entry = ttk.Entry(composer)
        self.chat_entry.pack(side='left', fill='x', expand=True, padx=(0, 5))
        self.chat_entry.bind('<Return>', self.send_chat)
        ttk.Button(composer, text='Send', style='Accent.TButton', command=self.send_chat).pack(side='right')

        footer = ttk.Frame(self.workspace)
        footer.pack(fill='x', pady=(10, 0))
        ttk.Label(footer, textvariable=self.status, foreground=MUTED).pack(side='left')
        ttk.Label(footer, textvariable=self.connection_label, foreground=ACCENT).pack(side='right')
        self.canvas.bind('<Configure>', self.resize)
        self.canvas.bind('<ButtonPress-1>', self.press)
        self.canvas.bind('<B1-Motion>', self.drag_motion)
        self.canvas.bind('<ButtonRelease-1>', self.release)
        self.canvas.bind('<Motion>', self.move_cursor)
        self.canvas.bind('<Leave>', self.leave_canvas)
        self.canvas.bind('<Escape>', self.cancel_drag)

    def enter(self, kind):
        host, port, name, title, code = (self.fields[key].get().strip()
                                         for key in ('address', 'port', 'name', 'title', 'code'))
        try:
            socket.inet_pton(socket.AF_INET, host)
            port = int(port)
            if not 1 <= port <= 65535:
                raise ValueError
        except (OSError, ValueError):
            self.status.set('Enter an IPv4 address and a port from 1 to 65535.')
            return
        if not name or (kind == 'create' and not title) or (kind == 'join' and not code):
            self.status.set('Enter your username and a room name or join code.')
            return
        self.leave()
        self.address = (host, port)
        self.code = code.upper() if kind == 'join' else ''
        self.hello = {'type': kind, 'name': name, 'title': title, 'code': self.code}
        self.status.set(f'Connecting to {host}:{port}…')
        self.connect(self.hello)

    def connect(self, hello):
        self.connection = Connection(self.address, hello)
        self.connection.start()

    def leave(self):
        if self.connection is not None:
            self.connection.stop()
        self.connection = self.user = self.token = None
        self.ready = self.syncing = False
        self.retry_at = False
        self.retry_delay = 1000
        self.save_path = self.drag = None
        self.cursor_point = self.sent_cursor = None
        self.pending_points.clear()
        self.items.clear()
        self.drafts.clear()
        self.people.clear()
        self.cursors.clear()
        self.replace_chat([])
        self.chat_entry.delete(0, 'end')
        self.workspace.pack_forget()
        self.lobby.pack(fill='both', expand=True)
        self.status.set('Enter a LAN address to begin.')

    def close(self):
        if self.connection is not None:
            self.connection.stop()
        self.root.destroy()

    def send(self, kind, numbered=False, **payload):
        if not self.ready or self.connection is None:
            return False
        if numbered:
            self.op += 1
            payload['op'] = self.op
        return self.connection.send({'type': kind, **payload})

    def poll(self):
        connection = self.connection
        if connection is not None:
            for _ in range(100):
                try:
                    message = connection.inbox.get_nowait()
                except Empty:
                    break
                self.on_message(message)
            if connection.closed.is_set() and connection.inbox.empty():
                self.connection = None
                self.ready = self.syncing = False
                self.save_path = None
                self.drag = None
                self.sent_cursor = None
                self.pending_points.clear()
                self.drafts.clear()
                self.cursors.clear()
                self.redraw()
                self.update_controls()
                if self.token:
                    self.status.set('Connection lost. Reconnecting…')
                    self.connection_label.set('Reconnecting')
                    self.retry_at = True
                    self.root.after(self.retry_delay, self.retry)
                    self.retry_delay = min(self.retry_delay * 2, 8000)
        self.root.after(20, self.poll)

    def retry(self):
        if self.retry_at and self.connection is None and self.token:
            self.retry_at = False
            self.connect({'type': 'join', 'name': self.user['name'], 'code': self.code, 'token': self.token})

    def on_message(self, message):
        kind = message['type']
        if kind == 'welcome':
            self.user, self.token, self.op = message['user'], message['token'], message['last_op']
            self.code = message.get('code', self.code)
            self.view_code = message.get('view_code', '')
            self.room_name.set(message['title'])
            host, port = self.address
            self.identity.set(f"{self.user['name']}  ·  {self.user['role'].title()}  ·  {host}:{port}")
            self.invite_text.set(f'Editor {self.code}   ·   Viewer {self.view_code}' if self.view_code else '')
            self.lobby.pack_forget()
            self.workspace.pack(fill='both', expand=True)
            self.update_controls()
        elif kind == 'snapshot':
            self.revision = message['rev']
            self.items = {item['id']: item for item in message['items']}
            self.drafts = {draft['user']: draft for draft in message['drafts']}
            if self.ready:
                if self.drag and self.drag['kind'] == 'pen' and self.user['id'] in self.drafts:
                    self.send('cancel')
                self.drag = None
                self.pending_points.clear()
            self.set_roster(message['participants'])
            self.replace_chat(message['chat'])
            self.ready = True
            self.syncing = False
            self.retry_delay = 1000
            self.status.set('Board synchronized.')
            self.set_revision()
            self.update_controls()
            self.redraw()
        elif kind == 'change':
            self.apply_change(message)
        elif kind == 'presence':
            self.set_roster(message['participants'])
            self.append_chat(message['notice'], notice=True)
            self.status.set(message['notice'])
        elif kind == 'draft':
            item = message['item']
            self.drafts[item['user']] = item
            self.canvas.delete(f"draft_{item['user']}")
            self.draw(item, f"draft_{item['user']}", ('drafts',))
        elif kind == 'draft_points':
            user_id = message['user']
            item = self.drafts.get(user_id)
            if item:
                segment = [item['points'][-1], *message['points']]
                item['points'].extend(message['points'])
                self.draw({**item, 'points': segment}, f'draft_{user_id}', ('drafts',))
                self.canvas.tag_raise('cursors')
        elif kind == 'draft_end':
            self.drafts.pop(message['user'], None)
            self.canvas.delete(f"draft_{message['user']}")
        elif kind == 'cursor':
            self.cursors[message['user']] = message['point']
            self.draw_cursor(message['user'])
        elif kind == 'cursor_end':
            self.cursors.pop(message['user'], None)
            self.canvas.delete(f"cursor_{message['user']}")
        elif kind == 'chat':
            self.chat_message(message['entry'])
        elif kind == 'saved':
            self.write_save(message['board'])
        elif kind == 'error':
            self.status.set(message['text'])
            if self.token and not self.ready and 'no longer be resumed' in message['text']:
                self.token = None
                self.connection_label.set('Disconnected')
            if self.token and not self.ready and 'Room code not found' in message['text']:
                self.token = None
                self.connection_label.set('Disconnected')

    def set_revision(self):
        self.connection_label.set(f'Connected  ·  Revision {self.revision}')

    def apply_change(self, message):
        revision, item_id, item = (message['rev'], message['id'], message['item'])
        if self.syncing or revision <= self.revision:
            return
        if revision != self.revision + 1:
            self.syncing = self.send('sync')
            return
        if item is None:
            self.items.pop(item_id, None)
            self.canvas.delete(f'item_{item_id}')
        elif item_id > max(self.items, default=0):
            self.items[item_id] = item
            self.draw(item, f'item_{item_id}', ('committed',))
            for tag in ('drafts', 'preview', 'cursors'):
                self.canvas.tag_raise(tag)
        elif item_id not in self.items:
            self.items[item_id] = item
            self.redraw()
        self.revision = revision
        self.set_revision()

    def update_controls(self):
        role = self.user['role'] if self.user else ''
        can_edit = self.ready and role != 'viewer'
        for widget in self.edit_widgets:
            widget.configure(state='normal' if can_edit else 'disabled')
        self.widths.configure(state='readonly' if can_edit else 'disabled')
        for widget in self.host_buttons:
            widget.configure(state='normal' if self.ready else 'disabled')
        if role == 'host':
            self.host_controls.pack(side='left', before=self.host_controls.master.winfo_children()[1])
        else:
            self.host_controls.pack_forget()

    def set_roster(self, participants):
        self.people = {person['id']: person for person in participants}
        self.roster_heading.configure(text=f'Participants ({len(self.people)})')
        self.roster.delete(0, 'end')
        for index, person in enumerate(participants):
            you = ' · you' if self.user and self.user['id'] == person['id'] else ''
            self.roster.insert('end', f"  ●  {person['name']}  ·  {person['role'].title()}{you}")
            self.roster.itemconfig(index, foreground=person['color'])
        for user_id in self.cursors.keys() - self.people.keys():
            del self.cursors[user_id]
            self.canvas.delete(f'cursor_{user_id}')

    def append_chat(self, text, tag=None, notice=False):
        self.chat.configure(state='normal')
        self.chat.insert('end', text + '\n', 'notice' if notice else (tag or ()))
        if int(self.chat.index('end-1c').split('.')[0]) > 450:
            self.chat.delete('1.0', '101.0')
        self.chat.see('end')
        self.chat.configure(state='disabled')

    def chat_message(self, entry):
        stamp = datetime.fromisoformat(entry['time']).astimezone().strftime('%H:%M:%S')
        self.append_chat(f"{entry['name']}  {stamp}", 'meta')
        self.append_chat(entry['text'])

    def replace_chat(self, entries):
        self.chat.configure(state='normal')
        self.chat.delete('1.0', 'end')
        self.chat.configure(state='disabled')
        for entry in entries:
            self.chat_message(entry)

    def send_chat(self, event=None):
        text = self.chat_entry.get().strip()
        if text and len(text) <= 400 and self.send('chat', numbered=True, text=text):
            self.chat_entry.delete(0, 'end')
        elif len(text) > 400:
            self.status.set('Chat messages can have up to 400 characters.')

    def copy_code(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.code)
        self.status.set('Editor code copied.')

    def clear(self):
        if self.ready and messagebox.askyesno('Clear board', 'Clear the board for everyone?', parent=self.root):
            self.send('clear', numbered=True)

    def save(self):
        if not self.ready or self.save_path is not None:
            return
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension='.json', filetypes=FILE_TYPES)
        if path and self.send('save'):
            self.save_path = Path(path)

    def write_save(self, board):
        if self.save_path is None:
            return
        path, self.save_path = self.save_path, None
        temporary_path = None
        try:
            # Write beside the destination so replacing it stays on the same drive.
            with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as file:
                temporary_path = Path(file.name)
                json.dump(board, file, ensure_ascii=False, separators=(',', ':'))
            temporary_path.replace(path)
            self.status.set(f'Saved {path.name}.')
        except OSError as exc:
            self.status.set(f'Could not save: {exc}')
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def restore(self):
        if not self.ready:
            return
        path = filedialog.askopenfilename(parent=self.root, filetypes=FILE_TYPES)
        if not path:
            return
        try:
            with Path(path).open('rb') as file:
                raw = file.read(MAX_FRAME + 1)
            if len(raw) > MAX_FRAME:
                raise ValueError('Board file is too large.')
            board = json.loads(raw.decode('utf-8'))
        except (OSError, ValueError, RecursionError) as exc:
            self.status.set(f'Could not open board: {exc}')
            return
        if messagebox.askyesno('Restore board', 'Replace the shared board with this file?', parent=self.root):
            self.send('restore', numbered=True, board=board)

    def select_color(self, color):
        self.color = color
        for swatch in self.swatches:
            swatch.configure(relief='sunken' if swatch.cget('bg') == color else 'raised')

    def pick_color(self):
        _, color = colorchooser.askcolor(self.color, parent=self.root)
        if color:
            self.select_color(color)

    def resize(self, event):
        self.scale = max(.01, min((event.width - 12) / BOARD_WIDTH, (event.height - 12) / BOARD_HEIGHT))
        self.offset_x = (event.width - BOARD_WIDTH * self.scale) / 2
        self.offset_y = (event.height - BOARD_HEIGHT * self.scale) / 2
        self.redraw()

    def screen(self, point):
        return self.offset_x + point[0] * self.scale, self.offset_y + point[1] * self.scale

    def board_point(self, event, clamp=False):
        x = round((event.x - self.offset_x) / self.scale)
        y = round((event.y - self.offset_y) / self.scale)
        if clamp:
            return [max(0, min(BOARD_WIDTH, x)), max(0, min(BOARD_HEIGHT, y))]
        if 0 <= x <= BOARD_WIDTH and 0 <= y <= BOARD_HEIGHT:
            return [x, y]
        return None

    def redraw(self):
        self.canvas.delete('all')
        self.canvas.create_rectangle(*self.screen((0, 0)), *self.screen((BOARD_WIDTH, BOARD_HEIGHT)),
                                     fill=PAPER, outline='#ccd9e0')
        for item_id, item in sorted(self.items.items()):
            self.draw(item, f'item_{item_id}', ('committed',))
        for user_id, draft in self.drafts.items():
            self.draw(draft, f'draft_{user_id}', ('drafts',))
        self.local_preview()
        for user_id in self.cursors:
            self.draw_cursor(user_id)

    def draw(self, item, tag, groups):
        tags = (tag, *groups)
        coords = [coordinate for point in item['points'] for coordinate in self.screen(point)]
        width = max(1, item['width'] * self.scale)
        color = item['color']
        kind = item['kind']
        if kind == 'note':
            x, y = coords
            self.canvas.create_rectangle(x, y, x + 175 * self.scale, y + 100 * self.scale,
                                         fill='#fff2b9', outline=color, width=max(1, self.scale), tags=tags)
            self.canvas.create_text(x + 10 * self.scale, y + 9 * self.scale, text=item['text'], anchor='nw',
                                    fill=INK, width=155 * self.scale, font=('TkDefaultFont', -max(6, round(13 * self.scale))), tags=tags)
        elif kind == 'rectangle':
            self.canvas.create_rectangle(*coords, outline=color, width=width, tags=tags)
        elif len(coords) == 2:
            x, y = coords
            r = width / 2
            self.canvas.create_oval(x - r, y - r, x + r, y + r, fill=color, outline='', tags=tags)
        else:
            self.canvas.create_line(*coords, fill=color, width=width, capstyle='round', joinstyle='round',
                                    arrow='last' if kind == 'arrow' else 'none', tags=tags)

    def draw_cursor(self, user_id):
        tag = f'cursor_{user_id}'
        self.canvas.delete(tag)
        person = self.people.get(user_id)
        if person:
            x, y = self.screen(self.cursors[user_id])
            tags = (tag, 'cursors')
            self.canvas.create_polygon(x, y, x + 3, y + 13, x + 11, y + 8, fill=person['color'], tags=tags)
            anchor = 'ne' if self.cursors[user_id][0] > BOARD_WIDTH - 130 else 'nw'
            self.canvas.create_text(x - 5 if anchor == 'ne' else x + 12, y + 7, anchor=anchor, text=person['name'],
                                    fill=person['color'], font=('TkDefaultFont', 9, 'bold'), tags=tags)

    def move_cursor(self, event):
        self.cursor_point = self.board_point(event)

    def leave_canvas(self, event):
        self.cursor_point = None

    def flush_pointer(self):
        if self.ready and self.cursor_point != self.sent_cursor:
            if self.cursor_point is None:
                self.send('cursor_end')
            else:
                self.send('cursor', point=self.cursor_point)
            self.sent_cursor = self.cursor_point
        self.flush_points()
        self.root.after(33, self.flush_pointer)

    def flush_points(self):
        while self.pending_points:
            batch = self.pending_points[:64]
            if not self.send('points', points=batch):
                break
            del self.pending_points[:len(batch)]

    def press(self, event):
        pos = self.board_point(event)
        if not pos or not self.ready or self.syncing or self.user['role'] == 'viewer':
            return
        self.canvas.focus_set()
        kind = self.tool.get().lower()
        if kind == 'eraser':
            for candidate in reversed(self.canvas.find_overlapping(event.x - 4, event.y - 4, event.x + 4, event.y + 4)):
                tags = self.canvas.gettags(candidate)
                if 'committed' in tags:
                    self.send('erase', numbered=True, id=int(tags[0].split('_')[1]))
                    break
            return
        item = {'kind': kind, 'color': self.color, 'width': int(self.width.get()), 'points': [pos]}
        if kind == 'note':
            value = simpledialog.askstring('Sticky note', 'Note text (up to 140 characters)', parent=self.root)
            if value and value.strip():
                text = value.strip()
                if len(text) > 140:
                    self.status.set('Sticky notes can have up to 140 characters.')
                    return
                item['text'] = text
                item['points'] = [[min(pos[0], BOARD_WIDTH - 175), min(pos[1], BOARD_HEIGHT - 100)]]
                self.send('add', numbered=True, item=item)
            return
        self.drag = item
        if kind == 'pen':
            self.send('start', item=item)
        else:
            item['points'].append(pos)
            self.local_preview()

    def drag_motion(self, event):
        self.move_cursor(event)
        if self.drag is None:
            return
        pos = self.board_point(event, clamp=True)
        points = self.drag['points']
        if points[-1] == pos:
            return
        if self.drag['kind'] == 'pen':
            segment = [points[-1], pos]
            points[-1] = pos
            self.pending_points.append(pos)
            self.draw({**self.drag, 'points': segment}, 'local_ink', ('preview',))
        else:
            points[-1] = pos
            self.local_preview()

    def local_preview(self):
        self.canvas.delete('local_shape')
        if self.drag and self.drag['kind'] != 'pen':
            self.draw(self.drag, 'local_shape', ('preview',))

    def release(self, event):
        if self.drag is None:
            return
        self.drag_motion(event)
        item, self.drag = self.drag, None
        self.canvas.delete('preview')
        if item['kind'] == 'pen':
            self.flush_points()
            self.send('finish', numbered=True)
        elif item['points'][0] != item['points'][1]:
            self.send('add', numbered=True, item=item)

    def cancel_drag(self, event):
        if self.drag and self.drag['kind'] == 'pen':
            self.send('cancel')
        self.drag = None
        self.pending_points.clear()
        self.canvas.delete('preview')


def main():
    root = tk.Tk()
    Whiteboard(root)
    root.mainloop()


if __name__ == '__main__':
    main()
