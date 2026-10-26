"""只访问 CLIPBOARD 的纯文本，以及单独启用时的 PNG/JPEG 图片；X11 用 xclip，Wayland 用 wl-clipboard。"""
import os
import selectors
import shutil
import socket
import struct
import subprocess
import sys
import time
from 剪贴板协议 import MAX_IMAGE_BYTES, MAX_RECEIVE_TEXT_BYTES, ClipboardImage, image_type

IMAGE_TYPES = ("image/png", "image/jpeg")
# 在 Xorg 桌面上，xclip 0.13 读取约 1 MiB 以上的选区会截断或卡住，还会让 xclip 提供方退出；
# xclip 提供大图时也会在 Klipper 抓取后退出，图片随之丢失。因此 X11 上图片的读写都用 GTK：
# 写入时由一个 GTK 进程持有选区，被其他复制取代后自行退出。
GTK_READ = (
    'import sys, gi\n'
    'gi.require_version("Gtk", "3.0")\n'
    'from gi.repository import Gtk, Gdk\n'
    'board = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)\n'
    'selection = board.wait_for_contents(Gdk.Atom.intern(sys.argv[1], False))\n'
    'data = selection.get_data() if selection is not None else None\n'
    'sys.exit(1) if not data else sys.stdout.buffer.write(data)\n')
GTK_WRITE = (
    'import os, sys, gi\n'
    'gi.require_version("Gtk", "3.0")\n'
    'from gi.repository import Gtk, Gdk\n'
    'kind = Gdk.Atom.intern(sys.argv[1], False)\n'
    'data = sys.stdin.buffer.read()\n'
    'owner = Gtk.Invisible()\n'
    'Gtk.selection_add_target(owner, Gdk.SELECTION_CLIPBOARD, kind, 0)\n'
    'owner.connect("selection-get", lambda _w, selection, _i, _t: selection.set(kind, 8, data))\n'
    'owner.connect("selection-clear-event", lambda *_: Gtk.main_quit())\n'
    'Gtk.selection_owner_set(owner, Gdk.SELECTION_CLIPBOARD, Gdk.CURRENT_TIME) or sys.exit(1)\n'
    'sys.stdout.write("ok"); sys.stdout.flush(); os.close(1)\n'
    'Gtk.main()\n')
# KDE Klipper 在剪贴板变空（原拥有者退出）时放回历史内容并带此标记；这不是新的复制，不发送。
RESTORED_MARKER = 'application/x-kde-onlyReplaceEmpty'
# 每次读取图片都要源应用重新编码；类型列表不变时最多每 5 秒重读一次。
IMAGE_REREAD_SECONDS = 5


class ClipboardChanged(RuntimeError):
    pass


def wayland_background_clipboard(env):
    """只查询注册表，不绑定剪贴板或创建窗口；探测失败时按不支持处理。"""
    display = env.get('WAYLAND_DISPLAY')
    if not display:
        return False
    if not os.path.isabs(display):
        runtime = env.get('XDG_RUNTIME_DIR')
        if not runtime:
            return False
        display = os.path.join(runtime, display)
    deadline = time.monotonic() + 2
    interfaces = {}

    def receive(connection, length):
        data = bytearray()
        while len(data) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            connection.settimeout(remaining)
            chunk = connection.recv(length - len(data))
            if not chunk:
                raise EOFError
            data.extend(chunk)
        return data

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(display)
            connection.settimeout(max(.01, deadline - time.monotonic()))
            # Wayland 使用本机字节序。仅发送 wl_display.get_registry 和 sync。
            # 新对象 2 是注册表，3 是标记初始公告结束的回调。
            connection.sendall(struct.pack('=IIIIII', 1, (12 << 16) | 1, 2, 1, 12 << 16, 3))
            total = 0
            while total < 256 * 1024:
                object_id, header = struct.unpack('=II', receive(connection, 8))
                size, opcode = header >> 16, header & 0xffff
                if size < 8 or size % 4:
                    return False
                body = receive(connection, size - 8)
                total += size
                if object_id == 1 and opcode == 0:  # wl_display.error
                    return False
                if object_id == 3 and opcode == 0 and len(body) == 4:
                    # 当前支持的 wl-clipboard 2.x 使用 wlr 数据控制协议。
                    return 'zwlr_data_control_manager_v1' in interfaces.values()
                if object_id == 2 and opcode == 0 and len(body) >= 12:
                    length = struct.unpack_from('=I', body, 4)[0]
                    if not length or 8 + ((length + 3) & ~3) + 4 != len(body) or body[8 + length - 1] != 0:
                        return False
                    name = struct.unpack_from('=I', body)[0]
                    version = struct.unpack_from('=I', body, len(body) - 4)[0]
                    if version:
                        interfaces[name] = bytes(body[8:8 + length - 1]).decode('ascii')
                elif object_id == 2 and opcode == 1 and len(body) == 4:
                    interfaces.pop(struct.unpack('=I', body)[0], None)
    except (OSError, EOFError, UnicodeError):
        return False
    return False


class DesktopClipboard:
    def __init__(self, backend='auto', env=None, images=False):
        self.env = dict(os.environ if env is None else env)
        self.images = images
        self.image_cache = None
        self.owner = None  # X11 上持有本机写入图片的 GTK 进程
        if backend not in ('auto', 'x11', 'wayland'):
            raise ValueError('剪贴板后端必须为 auto、x11 或 wayland')
        automatic = backend == 'auto'
        background = (wayland_background_clipboard(self.env)
                      if backend != 'x11' and self.env.get('WAYLAND_DISPLAY') else False)
        if automatic:
            backend = 'wayland' if background else 'x11'
        if self.env.get('WAYLAND_DISPLAY') and not background and backend == 'wayland':
            raise RuntimeError('当前 Wayland 桌面不支持后台剪贴板；请安装 xclip 并使用 --backend x11 或 auto（需要 XWayland）')
        if automatic and self.env.get('WAYLAND_DISPLAY') and backend == 'x11' and not self.env.get('DISPLAY'):
            raise RuntimeError('当前 Wayland 桌面不支持后台剪贴板，且未启用 XWayland；无法启用剪贴板同步')
        self.backend = backend
        display = 'WAYLAND_DISPLAY' if backend == 'wayland' else 'DISPLAY'
        if not self.env.get(display):
            raise RuntimeError('请在图形桌面的终端中启用剪贴板同步')
        required = ('wl-copy', 'wl-paste') if backend == 'wayland' else ('xclip',)
        self.tools = {}
        for name in required:
            tool = shutil.which(name, path=self.env.get('PATH'))
            if not tool:
                raise RuntimeError('缺少剪贴板工具：' + name)
            self.tools[name] = tool
        if images and backend == 'x11':
            probe = subprocess.run([sys.executable, '-c', GTK_READ.split('board')[0]], env=self.env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=10)
            if probe.returncode != 0:
                raise RuntimeError('X11 图片同步需要 python3-gi 和 GTK 3（Ubuntu：sudo apt install python3-gi gir1.2-gtk-3.0）')

    def capture(self, command, limit, allow_empty=False, timeout=2):
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=self.env)
        output = bytearray()
        errors = bytearray()
        deadline = time.monotonic() + timeout
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ, 'output')
        selector.register(proc.stderr, selectors.EVENT_READ, 'errors')
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                events = selector.select(max(0, remaining))
                if remaining <= 0 or not events:
                    raise TimeoutError('系统剪贴板读取超时')
                for key, _ in events:
                    target = output if key.data == 'output' else errors
                    cap = limit if key.data == 'output' else 4096
                    data = os.read(key.fileobj.fileno(), min(16384, cap + 1 - len(target)))
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    target.extend(data)
                    if len(target) > cap:
                        if key.data == 'output':
                            return None
                        raise RuntimeError('系统剪贴板工具异常')
            if proc.wait(timeout=max(.01, deadline - time.monotonic())) != 0:
                if allow_empty and (b'target TARGETS not available' in errors
                                    or errors.strip() in (b'No selection', b'Nothing is copied')):
                    return None
                raise RuntimeError('系统剪贴板读取失败')
            return bytes(output)
        finally:
            selector.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            proc.stderr.close()

    def read(self):
        if self.backend == 'wayland':
            paste = self.tools['wl-paste']
            offered = self.capture([paste, '--list-types'], 16384, allow_empty=True)
            if offered is None:
                return None
            types = offered.decode('ascii', errors='ignore').splitlines()
            candidates = ['text/plain;charset=utf-8', 'text/plain;charset=UTF-8', 'text/plain']
            target = next((kind for kind in candidates if kind in types), None)
            command = [paste, '--no-newline', '--type', target] if target else None
            image_command = lambda kind: [paste, '--type', kind]
        else:
            paste = [self.tools['xclip'], '-selection', 'clipboard', '-out']
            offered = self.capture(paste + ['-target', 'TARGETS'], 16384, allow_empty=True)
            if offered is None:
                return None
            types = offered.decode('ascii', errors='ignore').splitlines()
            target = next((kind for kind in ('UTF8_STRING', 'text/plain;charset=utf-8', 'text/plain', 'STRING') if kind in types), None)
            command = paste + ['-target', target] if target else None
            image_command = lambda kind: [sys.executable, '-c', GTK_READ, kind]
        if 'text/uri-list' in types or RESTORED_MARKER in types:
            return None
        if any(kind.startswith('image/') for kind in types):
            kind = next((kind for kind in IMAGE_TYPES if kind in types), None)
            return self.read_image(types, image_command(kind)) if self.images and kind else None
        if not command:
            return None
        data = self.capture(command, MAX_RECEIVE_TEXT_BYTES)
        if data is None:
            return None
        try:
            text = data.decode('utf-8')
        except UnicodeError:
            return None
        return text if '\0' not in text else None

    def read_image(self, types, command):
        """类型列表不变且未到重读间隔时沿用上次结果；自己写入的图片在类型不变时不再重读。
        超过上限、格式不符或数据不完整按无内容处理。"""
        key = tuple(types)
        now = time.monotonic()
        if self.image_cache and self.image_cache[0] == key and (
                self.image_cache[3] or now - self.image_cache[1] < IMAGE_REREAD_SECONDS):
            return self.image_cache[2]
        data = self.capture(command, MAX_IMAGE_BYTES, timeout=5)
        mime = image_type(data) if data else None
        image = ClipboardImage(data, mime) if mime else None
        self.image_cache = (key, now, image, False)
        return image

    def offered(self):
        if self.backend == 'wayland':
            data = self.capture([self.tools['wl-paste'], '--list-types'], 16384, allow_empty=True)
        else:
            data = self.capture([self.tools['xclip'], '-selection', 'clipboard', '-out', '-target', 'TARGETS'],
                                16384, allow_empty=True)
        return (data or b'').decode('ascii', errors='ignore').splitlines()

    def own_image(self, kind, data):
        """启动 GTK 进程持有图片，等它确认接管选区后返回；进程留在后台提供内容。"""
        if self.owner is not None:
            self.owner.poll()  # 回收已被其他复制取代而退出的上一个进程
        proc = subprocess.Popen([sys.executable, '-c', GTK_WRITE, kind], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=self.env)
        try:
            proc.stdin.write(data)
            proc.stdin.close()
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                ready = selector.select(5)
            if not ready or proc.stdout.read(2) != b'ok':
                raise RuntimeError('系统剪贴板未确认写入')
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            proc.stdout.close()
        self.owner = proc

    def write(self, value, expected):
        if self.read() != expected:
            raise ClipboardChanged('本地剪贴板已变化，本轮不覆盖')
        if isinstance(value, ClipboardImage):
            kind, data = value.mime, value.data
        else:
            kind, data = None, value.encode()
        self.image_cache = None
        if kind and self.backend == 'x11':
            self.own_image(kind, data)
            self.image_cache = (tuple(self.offered()), time.monotonic(), value, True)
            return
        if self.backend == 'wayland':
            command = [self.tools['wl-copy'], '--type', kind or 'text/plain;charset=utf-8']
        else:
            command = [self.tools['xclip'], '-selection', 'clipboard', '-in', '-target', 'UTF8_STRING']
        # 工具后台持有复制内容；正文只经 stdin，不出现在进程参数和日志中。
        subprocess.run(command, input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env=self.env, timeout=5, check=True)
        # xclip 先 fork 再接管选区，命令返回时新内容可能尚未生效。
        deadline = time.monotonic() + 1
        if kind:
            # 图片只核对类型列表，不回读整张图：回读大图既慢，又可能让提供方退出。
            while (types := self.offered()) != [kind]:
                if time.monotonic() > deadline:
                    raise RuntimeError('系统剪贴板未确认写入')
                time.sleep(.05)
            self.image_cache = (tuple(types), time.monotonic(), value, True)
            return
        while self.read() != value:
            if time.monotonic() > deadline:
                raise RuntimeError('系统剪贴板未确认写入')
            time.sleep(.05)
