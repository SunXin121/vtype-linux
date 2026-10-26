"""工具失败不是空剪贴板，使用合成命令验证失败恢复和读取上限。"""
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cloud'))
from 系统剪贴板 import DesktopClipboard, wayland_background_clipboard
from 剪贴板同步 import Synchronizer
from test_clipboard_sync import MemoryGroup


class DesktopClipboardTest(unittest.TestCase):
    def setUp(self):
        self.tools = patch('系统剪贴板.shutil.which', side_effect=lambda name, **kwargs: '/synthetic/' + name)
        self.tools.start()
        self.addCleanup(self.tools.stop)

    def test_auto_uses_xwayland_without_invoking_popup_tools(self):
        env = {'WAYLAND_DISPLAY': 'wayland-0', 'DISPLAY': ':0'}
        with patch('系统剪贴板.wayland_background_clipboard', return_value=False), \
                patch('系统剪贴板.subprocess.Popen') as popen:
            desktop = DesktopClipboard('auto', env)
        self.assertEqual(desktop.backend, 'x11')
        self.assertEqual(set(desktop.tools), {'xclip'})
        popen.assert_not_called()

    def test_background_wayland_is_preferred(self):
        with patch('系统剪贴板.wayland_background_clipboard', return_value=True):
            desktop = DesktopClipboard('auto', {'WAYLAND_DISPLAY': 'wayland-0', 'DISPLAY': ':0'})
        self.assertEqual(desktop.backend, 'wayland')

    def test_explicit_wayland_rejects_popup_fallback(self):
        with patch('系统剪贴板.wayland_background_clipboard', return_value=False), \
                patch('系统剪贴板.subprocess.Popen') as popen:
            with self.assertRaisesRegex(RuntimeError, '后台剪贴板'):
                DesktopClipboard('wayland', {'WAYLAND_DISPLAY': 'wayland-0', 'DISPLAY': ':0'})
        popen.assert_not_called()

    def test_no_xwayland_reports_unavailable_background_access(self):
        with patch('系统剪贴板.wayland_background_clipboard', return_value=False):
            with self.assertRaisesRegex(RuntimeError, '未启用 XWayland'):
                DesktopClipboard('auto', {'WAYLAND_DISPLAY': 'wayland-0'})

    def test_x11_does_not_probe_wayland(self):
        with patch('系统剪贴板.wayland_background_clipboard') as probe:
            self.assertEqual(DesktopClipboard('auto', {'DISPLAY': ':0'}).backend, 'x11')
            self.assertEqual(DesktopClipboard('x11', {'DISPLAY': ':0', 'WAYLAND_DISPLAY': 'wayland-0'}).backend, 'x11')
            with self.assertRaisesRegex(RuntimeError, '图形桌面'):
                DesktopClipboard('x11', {'WAYLAND_DISPLAY': 'wayland-0'})
        probe.assert_not_called()

    def test_missing_xclip_reports_required_fallback_tool(self):
        with patch('系统剪贴板.wayland_background_clipboard', return_value=False), \
                patch('系统剪贴板.shutil.which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'xclip'):
                DesktopClipboard('auto', {'DISPLAY': ':0', 'WAYLAND_DISPLAY': 'wayland-0'})

    def test_empty_wayland_selection_is_a_valid_baseline(self):
        self.tools.stop()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('wl-paste', 'wl-copy'):
                script = root / name
                script.write_text(f'#!{sys.executable}\nimport sys\nprint("Nothing is copied",file=sys.stderr);sys.exit(1)\n')
                script.chmod(0o700)
            with patch('系统剪贴板.wayland_background_clipboard', return_value=True):
                desktop = DesktopClipboard('wayland', dict(os.environ, PATH=str(root), WAYLAND_DISPLAY='synthetic'))
            self.assertIsNone(desktop.read())

    def test_failed_first_tool_read_does_not_upload_preexisting_text_after_recovery(self):
        self.tools.stop()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'xclip'
            script.write_text(f'#!{sys.executable}\nimport os,sys\n'
                              'if os.path.exists(os.environ["SYNTHETIC_FAILURE"]):\n'
                              '    print("synthetic read failure",file=sys.stderr);sys.exit(1)\n'
                              'print("UTF8_STRING") if "TARGETS" in sys.argv else sys.stdout.write("preexisting")\n')
            script.chmod(0o700)
            failure = root / 'failure'
            failure.touch()
            env = dict(os.environ, PATH=str(root), DISPLAY=':synthetic', SYNTHETIC_FAILURE=str(failure))
            desktop = DesktopClipboard('x11', env)
            group = MemoryGroup()
            sync = Synchronizer(desktop, group, root / 'cursor.json')
            with self.assertRaises(RuntimeError):
                sync.step()
            failure.unlink()
            sync.step()
            self.assertEqual(group.sent, [])

    def test_x11_images_require_gtk_reader(self):
        self.tools.stop()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'xclip'
            script.write_text(f'#!{sys.executable}\nimport sys\nprint("UTF8_STRING")\n')
            script.chmod(0o700)
            env = dict(os.environ, PATH=str(root), DISPLAY=':synthetic')
            failed = subprocess.CompletedProcess([], 1)
            with patch('系统剪贴板.subprocess.run', return_value=failed), self.assertRaisesRegex(RuntimeError, 'python3-gi'):
                DesktopClipboard('x11', env, images=True)
            with patch('系统剪贴板.subprocess.run', return_value=failed):
                self.assertEqual(DesktopClipboard('x11', env).backend, 'x11')

    def test_klipper_restored_content_is_not_treated_as_a_new_copy(self):
        self.tools.stop()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'xclip'
            script.write_text(f'#!{sys.executable}\nimport sys\n'
                              'print("UTF8_STRING\\napplication/x-kde-onlyReplaceEmpty\\nimage/png") if "TARGETS" in sys.argv '
                              'else sys.stdout.write("restored")\n')
            script.chmod(0o700)
            env = dict(os.environ, PATH=str(root), DISPLAY=':synthetic')
            self.assertIsNone(DesktopClipboard('x11', env).read())
            self.assertIsNone(DesktopClipboard('x11', env, images=True).read())


class WaylandProbeTest(unittest.TestCase):
    @staticmethod
    def event(object_id, opcode, body):
        return struct.pack('=II', object_id, ((8 + len(body)) << 16) | opcode) + body

    def probe(self, interface=None, failure=None):
        response = bytearray()
        if interface:
            name = interface.encode() + b'\0'
            body = struct.pack('=II', 1, len(name)) + name + b'\0' * (-len(name) % 4) + struct.pack('=I', 1)
            response.extend(self.event(2, 0, body))
        response.extend(self.event(3, 0, struct.pack('=I', 0)))
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)

        def receive(length):
            if failure:
                raise failure
            # 刻意拆分消息，验证流式传输，不依赖一次 recv 得到完整事件。
            data = bytes(response[:min(length, 3)])
            del response[:len(data)]
            return data

        connection.recv.side_effect = receive
        with patch('系统剪贴板.socket.socket', return_value=connection):
            supported = wayland_background_clipboard({'WAYLAND_DISPLAY': 'wayland-0', 'XDG_RUNTIME_DIR': '/synthetic'})
        connection.connect.assert_called_once_with('/synthetic/wayland-0')
        connection.sendall.assert_called_once_with(struct.pack('=IIIIII', 1, (12 << 16) | 1, 2, 1, 12 << 16, 3))
        connection.__exit__.assert_called_once()
        return supported

    def test_registry_only_probe_supports_wlr_data_control(self):
        self.assertTrue(self.probe('zwlr_data_control_manager_v1'))

    def test_core_clipboard_protocol_cannot_support_background_access(self):
        self.assertFalse(self.probe('wl_data_device_manager'))

    def test_timeout_and_disconnect_fail_closed(self):
        self.assertFalse(self.probe(failure=socket.timeout()))
        self.assertFalse(self.probe(failure=EOFError()))

    def test_missing_runtime_does_not_connect(self):
        with patch('系统剪贴板.socket.socket') as connection:
            self.assertFalse(wayland_background_clipboard({'WAYLAND_DISPLAY': 'wayland-0'}))
        connection.assert_not_called()


if __name__ == '__main__':
    unittest.main()
