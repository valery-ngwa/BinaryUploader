"""Mock tests for command construction, file checks, and serial handoff.

These tests do not erase or flash a connected board.
"""

import queue
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_flasher as flasher


def merged_image(directory, name="image-serial.bin", size=0x10001):
    data = bytearray(size)
    data[0] = 0xE9
    data[0x8000:0x8002] = b"\xaa\x50"
    data[0x10000] = 0xE9
    path = Path(directory) / name
    path.write_bytes(data)
    return path


class FakeSerial:
    def __init__(self):
        self.closed = False
        self.written = []
        self.reads = queue.Queue()

    def readline(self):
        if self.closed:
            raise OSError("closed")
        try:
            return self.reads.get(timeout=0.02)
        except queue.Empty:
            return b""

    def write(self, data):
        self.written.append(bytes(data))
        return len(data)

    def close(self):
        self.closed = True
        self.reads.put(b"")


class CommandTests(unittest.TestCase):
    def test_backup_uses_detected_size(self):
        command = flasher.backup_command(sys.executable, "COM32", "auto", 8 * 1024 * 1024, Path("backup.bin"))
        size_argument = command[command.index("read-flash") + 2]
        self.assertEqual(size_argument, hex(8 * 1024 * 1024))
        self.assertNotEqual(size_argument, hex(16 * 1024 * 1024))
        self.assertEqual(command[command.index("--chip") + 1], "auto")
        self.assertEqual(command[command.index("--port") + 1], "COM32")

    def test_backup_refuses_unknown_size(self):
        with self.assertRaises(ValueError):
            flasher.backup_command(sys.executable, "COM32", "esp32s3", None, Path("backup.bin"))

    def test_erase_is_separate_from_flash(self):
        erase = flasher.erase_command(sys.executable, "com5 — bridge", "esp32s3")
        self.assertEqual(erase[-1], "erase-flash")
        self.assertNotIn("write-flash", erase)
        self.assertEqual(erase[erase.index("--port") + 1], "COM5")

    def test_standalone_bin_keeps_its_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            firmware = Path(tmp) / "firmware.bin"
            firmware.write_bytes(b"\xe9firmware")
            command = flasher.flash_parts_command(
                sys.executable, "COM32", "esp32s3", [(0x10000, firmware)]
            )
        addresses = [item for item in command if item.startswith("0x")]
        self.assertEqual(addresses, ["0x10000"])
        self.assertEqual(command[-2:], ["0x10000", str(firmware)])

    def test_parts_require_an_address_and_reject_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            firmware = Path(tmp) / "firmware.bin"
            firmware.write_bytes(b"abc")
            with self.assertRaises(ValueError):
                flasher.flash_parts_command(sys.executable, "COM32", "auto", [])
            with self.assertRaises(ValueError):
                flasher.flash_parts_command(
                    sys.executable,
                    "COM32",
                    "auto",
                    [(0x10000, firmware), (0x10000, firmware)],
                )

    def test_merged_image_is_written_at_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = merged_image(tmp)
            command = flasher.merged_flash_command(sys.executable, "COM32", "auto", image)
        self.assertEqual(command[-3:], ["write-flash", "0x0", str(image)])

    def test_ordinary_bin_is_not_treated_as_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            firmware = Path(tmp) / "firmware.bin"
            firmware.write_bytes(b"\xe9" + b"\x00" * 200)
            with self.assertRaises(ValueError) as caught:
                flasher.merged_flash_command(sys.executable, "COM32", "auto", firmware)
        self.assertIn("explicit flash address", str(caught.exception))

    def test_two_projects_upload_to_the_same_port(self):
        cold = flasher.platformio_command(["pio"], Path("cold-room"), "cold-room", "COM32")
        drying = flasher.platformio_command(["pio"], Path("drying-room"), "drying-room", "COM32")
        build = flasher.platformio_command(["pio"], Path("cold-room"), "cold-room")
        self.assertEqual(cold[-2:], ["--upload-port", "COM32"])
        self.assertEqual(drying[-2:], ["--upload-port", "COM32"])
        self.assertIn(str(Path("cold-room")), cold)
        self.assertIn(str(Path("drying-room")), drying)
        self.assertNotIn("--upload-port", build)
        self.assertNotIn("-t", build)

    def test_platformio_envs_come_from_each_project(self):
        text = "[platformio]\n[env]\n[env:cold-room]\n[env:drying-room]\n; [env:ignored]\n"
        self.assertEqual(flasher.parse_platformio_envs(text), ["cold-room", "drying-room"])

    def test_find_platformio_reports_a_missing_tool(self):
        with patch("bench_flasher.platformio_candidates", return_value=[]):
            with patch("bench_flasher.importlib.util.find_spec", return_value=None):
                with self.assertRaises(FileNotFoundError) as caught:
                    flasher.find_platformio(None)
        self.assertIn("platformio.exe", str(caught.exception))

    def test_penv_install_is_preferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            penv = home / ".platformio" / "penv" / "Scripts" / "platformio.exe"
            penv.parent.mkdir(parents=True)
            penv.write_bytes(b"")
            other = home / "Scripts" / "platformio.exe"
            other.parent.mkdir()
            other.write_bytes(b"")
            with patch("bench_flasher.Path.home", return_value=home):
                with patch("bench_flasher.shutil.which", return_value=str(other)):
                    self.assertEqual(flasher.find_platformio(None), [str(penv)])

    def test_find_platformio_uses_an_explicit_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "platformio.exe"
            exe.write_bytes(b"")
            self.assertEqual(flasher.find_platformio(str(exe)), [str(exe)])
            with self.assertRaises(FileNotFoundError):
                flasher.find_platformio(str(Path(tmp) / "missing.exe"))


class FileValidationTests(unittest.TestCase):
    def test_flash_size_parser_prefers_the_detected_line(self):
        output = "Embedded Flash 8MB\nDetected flash size: 16MB\n"
        self.assertEqual(flasher.parse_flash_size(output), 16 * 1024 * 1024)
        self.assertEqual(flasher.parse_flash_size("Flash size: 4 MB"), 4 * 1024 * 1024)
        with self.assertRaises(ValueError):
            flasher.parse_flash_size("Chip is ESP32-S3")

    def test_address_is_required(self):
        self.assertEqual(flasher.parse_address("0x10000"), 0x10000)
        self.assertEqual(flasher.parse_address("0x0"), 0)
        with self.assertRaises(ValueError):
            flasher.parse_address("  ")
        with self.assertRaises(ValueError):
            flasher.parse_address("boot")

    def test_manifest_imports_addresses_and_resolves_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            bootloader = folder / "bootloader.bin"
            firmware = folder / "firmware.bin"
            bootloader.write_bytes(b"boot")
            firmware.write_bytes(b"app")
            manifest = folder / "flasher_args.json"
            manifest.write_text(
                '{"flash_files": {"0x10000": "firmware.bin", "0x0": "bootloader.bin"}}',
                encoding="utf-8",
            )
            parts = flasher.load_flash_manifest(manifest)
            self.assertEqual(parts, [(0, bootloader), (0x10000, firmware)])
            manifest.write_text('{"flash_files": {"0x10000": "missing.bin"}}', encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                flasher.load_flash_manifest(manifest)
        self.assertIn("missing", str(caught.exception))

    def test_restore_size_must_match_the_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "controller-backup.bin"
            backup.write_bytes(b"\x00" * (8 * 1024 * 1024))
            flasher.check_backup(backup, 8 * 1024 * 1024)
            with self.assertRaises(ValueError) as caught:
                flasher.check_backup(backup, 16 * 1024 * 1024)
        self.assertIn("connected device", str(caught.exception))

    def test_merged_magic_and_oversize_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = merged_image(tmp, size=0x10001)
            flasher.check_merged_image(image)
            with self.assertRaises(ValueError):
                flasher.check_merged_image(image, flash_size=0x8000)

    def test_tool_errors_are_actionable(self):
        missing = flasher.explain_tool_error("Erase", "No module named esptool", 1)
        busy = flasher.explain_tool_error("Upload", "Could not open port COM32: Access is denied", 2)
        build = flasher.explain_tool_error("Build", "error: unknown type", 1)
        self.assertIn("pip install esptool", missing)
        self.assertIn("serial port is in use", busy)
        self.assertIn("Build failed", build)

    def test_line_endings(self):
        self.assertEqual(flasher.line_ending_bytes("None"), b"")
        self.assertEqual(flasher.line_ending_bytes("LF"), b"\n")
        self.assertEqual(flasher.line_ending_bytes("CR"), b"\r")
        self.assertEqual(flasher.line_ending_bytes("CRLF"), b"\r\n")


class SerialHandoffTests(unittest.TestCase):
    def wait_for(self, events, kind, timeout=2):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                event = events.get(timeout=0.05)
            except queue.Empty:
                continue
            if event[0] == kind:
                return event
        self.fail(f"timed out waiting for {kind}")

    def test_operation_releases_the_monitor_and_restores_it(self):
        events = queue.Queue()
        opened = []

        def factory(port, baud):
            serial_port = FakeSerial()
            opened.append((port, baud, serial_port))
            return serial_port

        session = flasher.PortSession(flasher.Monitor(events, open_port=factory))
        session.connect_monitor("COM32", 115200, "CRLF", retry_delay=0.01)
        self.wait_for(events, "monitor-connected")
        self.assertTrue(opened[0][2].closed is False)

        resume = session.acquire()
        self.assertEqual(resume, ("COM32", 115200, "CRLF"))
        self.assertTrue(opened[0][2].closed)
        self.assertEqual(session.owner, "operation")
        with self.assertRaises(RuntimeError):
            session.acquire()
        with self.assertRaises(RuntimeError):
            session.monitor.send("hi", "LF")
        with self.assertRaises(RuntimeError):
            session.connect_monitor("COM32", 115200, "LF")

        session.release(resume)
        self.wait_for(events, "monitor-connected")
        self.assertEqual(session.monitor.settings, ("COM32", 115200, "CRLF"))
        self.assertFalse(opened[1][2].closed)
        session.monitor.send("status", "CRLF")
        deadline = time.time() + 2
        while time.time() < deadline and opened[1][2].written != [b"status\r\n"]:
            time.sleep(0.02)
        self.assertEqual(opened[1][2].written, [b"status\r\n"])
        session.disconnect_monitor()

    def test_acquire_without_a_monitor_does_not_reconnect(self):
        events = queue.Queue()
        session = flasher.PortSession(flasher.Monitor(events, open_port=FakeSerial))
        self.assertIsNone(session.acquire())
        self.assertEqual(session.owner, "operation")
        session.release(None)
        self.assertIsNone(session.owner)
        self.assertFalse(session.monitor.active)

    def test_open_retries_then_connects(self):
        events = queue.Queue()
        attempts = {"count": 0}

        def factory(port, baud):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("could not open port COM32: Access is denied")
            return FakeSerial()

        monitor = flasher.Monitor(events, open_port=factory)
        monitor.connect("COM32", 115200, "LF", retries=2, retry_delay=0.01)
        self.wait_for(events, "monitor-connected")
        self.assertEqual(attempts["count"], 2)
        monitor.disconnect()


class ProcessStreamTests(unittest.TestCase):
    def test_stream_collects_output_without_touching_a_board(self):
        lines = []
        code = flasher.stream_process([sys.executable, "-c", "print('ready')"], lines.append)
        self.assertEqual(code, 0)
        self.assertEqual("".join(lines).strip(), "ready")


class WindowTests(unittest.TestCase):
    def test_window_builds_without_flashing(self):
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        try:
            with patch("bench_flasher.load_settings", return_value={}):
                app = flasher.Flasher(root)
            root.update_idletasks()
            self.assertEqual(app.chip.get(), "auto")
            self.assertIn("Build", app.build_button.cget("text"))
            self.assertIn("Flash", app.flash_button.cget("text"))
            self.assertEqual(app.ending.get(), "LF")
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
