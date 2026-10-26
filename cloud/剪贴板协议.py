"""官方设备组的文本与图片剪贴板传输，协议依据见剪贴板文档。"""
from dataclasses import dataclass
import base64
import fcntl
import hashlib
import json
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path
import 官方同步探针 as p

# 发送只走内联文本；更长的文本官方改用 COS 链接，Linux 只接收不上传。
MAX_TEXT_BYTES = 256 * 1024
MAX_RECEIVE_TEXT_BYTES = 1024 * 1024
TEXT_INLINE = 1
TEXT_COS = 2
# 图片以 AES-128-CTR 加密后放在 COS，消息里只带地址、原图 MD5 和密钥。
# 2026-10-26 实测服务器上传上限为 10 MiB；接收按 wx-ime-sdk 的 16 MiB。
IMAGE_COS = 6
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_RECEIVE_IMAGE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class ClipboardImage:
    data: bytes
    mime: str


def image_type(data):
    """只认完整的 PNG 和 JPEG，与官方客户端互通的格式一致；读取被截断的数据不算图片。"""
    if data.startswith(b'\x89PNG\r\n\x1a\n') and data.endswith(b'IEND\xaeB`\x82'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff') and data.rstrip(b'\0').endswith(b'\xff\xd9'):
        return 'image/jpeg'
    return None


def image_cipher(key, data):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    # 与官方一致：密钥同时用作计数器初值，加密和解密是同一运算。
    cipher = Cipher(algorithms.AES(key), modes.CTR(key)).encryptor()
    return cipher.update(data) + cipher.finalize()


def image_reference(data):
    """远端图片引用不可信：版本、MD5、密钥和地址都核对后才使用。"""
    try:
        value = json.loads(data)
        key = base64.b64decode(value['key'], validate=True)
        md5 = value['md5'].lower()
        if (value.get('version') != 1 or len(key) != 16 or len(md5) != 32
                or any(c not in '0123456789abcdef' for c in md5)):
            return None
        return {'url': cos_url(value['path']), 'md5': md5, 'key': key}
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


class SendUnknown(RuntimeError):
    """发送请求已经开始，无法确认服务器是否收到了文本。"""


class NotSent(RuntimeError):
    """发送前检查失败，本次没有发送文本。"""


@dataclass(frozen=True)
class ClipboardMessage:
    version: int
    source: int
    expires: int
    kind: int
    text: str
    trace: str
    url: str | None = None
    image: dict | None = None


@dataclass(frozen=True)
class GroupSnapshot:
    group: int
    own: int
    members: frozenset
    message: ClipboardMessage | None
    now_ms: int


def decode_text(data):
    try:
        text = data.decode('utf-8')
    except UnicodeError:
        return ''
    return text if '\0' not in text else ''


def cos_url(value):
    """只接受官方 COS 的 HTTPS 地址，与 wx-ime-sdk 的校验一致。"""
    url = urllib.parse.urlsplit(value)
    if (url.scheme != 'https' or url.username or url.password or url.port not in (None, 443)
            or not (url.hostname or '').endswith('.myqcloud.com') or url.fragment):
        raise ValueError('剪贴板链接不是官方 COS 地址')
    return value


def download(url, limit, opener=urllib.request.urlopen, timeout=10):
    with opener(urllib.request.Request(cos_url(url)), timeout=timeout) as response:
        if response.status != 200:
            raise ValueError('剪贴板内容下载失败')
        data = response.read(limit + 1)
    if not data or len(data) > limit:
        raise ValueError('剪贴板内容为空或超过上限')
    return data


def download_text(url, limit=MAX_RECEIVE_TEXT_BYTES, opener=urllib.request.urlopen):
    return decode_text(download(url, limit, opener))


def download_image(reference, opener=urllib.request.urlopen):
    """返回解密后的图片；MD5 不符或格式不支持时返回 None，不让坏消息反复重试。"""
    data = image_cipher(reference['key'], download(reference['url'], MAX_RECEIVE_IMAGE_BYTES, opener, timeout=30))
    mime = image_type(data)
    if hashlib.md5(data).hexdigest() != reference['md5'] or not mime:
        return None
    return ClipboardImage(data, mime)


def upload(url, data, opener=urllib.request.urlopen):
    request = urllib.request.Request(cos_url(url), data=data, method='PUT',
                                     headers={'Content-Type': 'application/octet-stream'})
    with opener(request, timeout=30) as response:
        if response.status not in (200, 201, 204):
            raise ValueError('剪贴板图片上传失败')


class OfficialClipboard:
    """只在读取身份时持锁，网络请求不占锁，避免挡住打字时的云候选；发送前重新验证组号与成员。"""
    def __init__(self, state, probe_factory=p.Probe):
        self.state = Path(state)
        self.probe_factory = probe_factory
        self.client = None
        self.calibrated = 0

    def session(self):
        from contextlib import contextmanager
        @contextmanager
        def locked():
            with (self.state / 'identity.lock').open('a+') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                identity = json.loads((self.state / 'identity.json').read_text())
            if (self.client is None or self.client.identity.get('server_key') != identity.get('server_key')
                    or self.client.identity.get('uin') != identity.get('uin')):
                self.client = self.probe_factory(identity)
                self.client.timeout = 2
                self.calibrated = 0
            if time.monotonic() - self.calibrated > 60:
                started = int(time.time() * 1000)
                stamp = p.one(p.fields(self.client.request('/timestamp', b'')), 1)
                self.client.offset = stamp - (started + int(time.time() * 1000)) // 2
                self.calibrated = time.monotonic()
            yield self.client
        return locked()

    def group(self, client, include_clipboard):
        own = client.identity['uin']
        request = p.number(1, own) + p.number(2, 1) + p.number(3, 3 if include_clipboard else 1)
        if include_clipboard:
            request += p.number(4, 0)
        response = p.fields(client.api(34, request, persist_errors=False))
        if p.one(response, 2) != 0:
            raise NotSent('设备组查询失败')
        group = p.fields(p.one(response, 1, b''))
        group_id = p.one(group, 1)
        members = frozenset(p.one(p.fields(raw), 1) for raw in group.get(2, []))
        if not group_id or not own or own not in members or not any(member and member != own for member in members):
            raise NotSent('设备尚未关联到有效设备组')
        raw_message = p.one(group, 3, b'')
        message = None
        if raw_message:
            fields = p.fields(raw_message)
            kind = p.one(fields, 2)
            data = p.one(fields, 6, b'') if kind in (TEXT_INLINE, TEXT_COS, IMAGE_COS) else b''
            text, url, image = '', None, None
            if kind == TEXT_INLINE and len(data) <= MAX_RECEIVE_TEXT_BYTES:
                text = decode_text(data)
            elif kind == TEXT_COS and len(data) <= 4096:
                url = decode_text(data) or None
            elif kind == IMAGE_COS and len(data) <= 4096:
                image = image_reference(data)
            # 远端内容不可信：无法解码的消息按非文本处理，不让异常卡住两个方向。
            message = ClipboardMessage(p.one(fields, 1), p.one(fields, 7), p.one(fields, 4), kind,
                                       text, decode_text(p.one(fields, 9, b'')), url, image)
        return GroupSnapshot(group_id, own, members, message, client.timestamp())

    def text(self, message):
        """返回消息的文本；COS 长文本此时才下载，失败时抛出异常并保留游标。"""
        if message.kind == TEXT_COS:
            return download_text(message.url) if message.url else ''
        return message.text

    def content(self, message):
        """返回文本或 ClipboardImage；图片此时才下载解密，网络失败时抛出异常并保留游标。"""
        if message.kind == IMAGE_COS:
            return download_image(message.image) if message.image else None
        return self.text(message)

    def fetch(self):
        with self.session() as client:
            return self.group(client, True)

    def send(self, snapshot, value, trace):
        if isinstance(value, ClipboardImage):
            return self.send_image(snapshot, value, trace)
        text = value
        if not text or '\0' in text or len(text.encode()) > MAX_TEXT_BYTES:
            raise NotSent('文本必须为 1 到 256 KiB 的 UTF-8 内容')
        return self.publish(snapshot, p.number(2, TEXT_INLINE) + p.blob(6, text) + p.blob(9, trace))

    def send_image(self, snapshot, image, trace, opener=urllib.request.urlopen):
        if not image.data or len(image.data) > MAX_IMAGE_BYTES or image_type(image.data) != image.mime:
            raise NotSent('图片必须为 10 MiB 以内的 PNG 或 JPEG')
        key = secrets.token_bytes(16)
        try:
            with self.session() as client:
                extra = json.dumps({'log_fully_custom_path': str(client.timestamp())})
                credential = p.fields(client.api(60, p.number(1, 6) + p.blob(2, extra) + p.number(3, 3),
                                                 persist_errors=False))
            target = p.one(credential, 5, b'').decode()
            source = p.one(credential, 4, b'').decode()
            limit = p.one(credential, 6) * 1024
            if not limit or len(image.data) > limit:
                raise NotSent('图片超过服务器上限')
            upload(target, image_cipher(key, image.data), opener)
            reference = json.dumps({'version': 1, 'path': cos_url(source),
                                    'md5': hashlib.md5(image.data).hexdigest(),
                                    'key': base64.b64encode(key).decode()})
        except NotSent:
            raise
        except Exception:
            # 上传未发布的对象不会被任何设备读取，按未发送处理。
            raise NotSent('图片上传失败，本次未发送') from None
        return self.publish(snapshot, p.number(2, IMAGE_COS) + p.blob(6, reference) + p.blob(9, trace))

    def publish(self, snapshot, message):
        try:
            with self.session() as client:
                current = self.group(client, False)
                if (current.group, current.own) != (snapshot.group, snapshot.own):
                    raise NotSent('设备组已变化，本次未发送')
                request = p.number(1, current.own) + p.number(2, 1) + p.blob(3, message) + p.number(4, current.group)
                try:
                    response = p.fields(client.api(36, request, persist_errors=False))
                    if p.one(response, 1) != 0 or p.one(response, 2) <= 0:
                        raise SendUnknown('服务器没有确认发送版本')
                    return p.one(response, 2)
                except Exception:
                    raise SendUnknown('发送结果不明，等待核对远端状态') from None
        except (NotSent, SendUnknown):
            raise
        except Exception:
            raise NotSent('发送前连接或身份检查失败') from None
