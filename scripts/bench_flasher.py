"""Windows desktop tool for developing and testing ESP boards."""

from __future__ import annotations

import importlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

BACKUP_NAME = "controller-backup.bin"
MAX_FLASH = 128 * 1024 * 1024
CHIPS = (
    "auto",
    "esp8266",
    "esp32",
    "esp32s2",
    "esp32s3",
    "esp32c2",
    "esp32c3",
    "esp32c6",
    "esp32h2",
    "esp32p4",
)
BAUD_RATES = ("9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600")
ENDINGS = {"None": b"", "LF": b"\n", "CR": b"\r", "CRLF": b"\r\n"}
MERGED_ERROR = (
    "Choose a merged serial image for address 0x0, such as *-serial.bin. "
    "A standalone .bin needs an explicit flash address: add it under Separate files "
    "or import the build manifest."
)
_ENV = re.compile(r"^\[env:([^\]\s]+)\]", re.MULTILINE)
_FLASH_SIZE = re.compile(r"(?:Detected flash size|Flash size):\s*(\d+)\s*(KB|MB)\b", re.IGNORECASE)
_BUSY_PORT = (
    "could not open port",
    "access is denied",
    "permissionerror",
    "port is busy",
    "resource busy",
    "device not configured",
)


def esptool_prefix(python, port, chip):
    if chip not in CHIPS:
        raise ValueError("Select an ESP chip, or use auto.")
    return [python, "-u", "-m", "esptool", "--chip", chip, "--port", normalize_port(port)]


def backup_command(python, port, chip, flash_size, path):
    if not isinstance(flash_size, int) or flash_size <= 0:
        raise ValueError("Detect the device flash size before backing up.")
    return esptool_prefix(python, port, chip) + ["read-flash", "0x0", hex(flash_size), str(path)]


def restore_command(python, port, chip, path):
    return esptool_prefix(python, port, chip) + ["write-flash", "0x0", str(path)]


def erase_command(python, port, chip):
    return esptool_prefix(python, port, chip) + ["erase-flash"]


def detect_command(python, port, chip):
    return esptool_prefix(python, port, chip) + ["flash-id"]


def flash_parts_command(python, port, chip, parts):
    if not parts:
        raise ValueError(
            "Add at least one .bin with a flash address, or import the build manifest. "
            "A standalone .bin is not assumed to belong at 0x0."
        )
    normalized = []
    seen = set()
    for address, file in parts:
        if not isinstance(address, int) or address < 0:
            raise ValueError("Each flash file needs an explicit address, such as 0x10000.")
        if address in seen:
            raise ValueError(
                f"{hex(address)} is used by more than one file. Remove the duplicate before flashing."
            )
        seen.add(address)
        path = Path(file)
        if not path.is_file():
            raise ValueError(f"Flash file is missing: {path}")
        normalized.append((address, path))
    normalized.sort(key=lambda item: item[0])
    command = esptool_prefix(python, port, chip) + ["write-flash"]
    for address, path in normalized:
        command.extend((hex(address), str(path)))
    return command


def merged_flash_command(python, port, chip, path, flash_size=None):
    check_merged_image(path, flash_size)
    return esptool_prefix(python, port, chip) + ["write-flash", "0x0", str(path)]


def platformio_command(executable, project, environment, upload_port=None):
    if not project:
        raise ValueError("Browse to a PlatformIO project folder.")
    if not environment:
        raise ValueError("Select a build environment from platformio.ini.")
    command = [*executable, "run", "-d", str(project), "-e", environment]
    if upload_port:
        command += ["-t", "upload", "--upload-port", normalize_port(upload_port)]
    return command


def parse_platformio_envs(text):
    return _ENV.findall(text)


def project_environments(folder):
    ini = Path(folder) / "platformio.ini"
    if not ini.is_file():
        raise ValueError(
            f"No platformio.ini in {folder}. Choose the cold-room or drying-room project folder."
        )
    envs = parse_platformio_envs(ini.read_text(encoding="utf-8", errors="replace"))
    if not envs:
        raise ValueError("platformio.ini has no [env:name] section. Add a build environment and try again.")
    return envs


def parse_flash_size(output):
    match = _FLASH_SIZE.search(output)
    if not match:
        raise ValueError(
            "Could not read the flash size from esptool. "
            "Check the COM port and chip selection, then use Detect flash."
        )
    amount = int(match.group(1))
    unit = match.group(2).upper()
    return amount * 1024 * 1024 if unit == "MB" else amount * 1024


def parse_address(text):
    raw = str(text).strip()
    if not raw:
        raise ValueError("Enter a flash address, such as 0x10000.")
    try:
        value = int(raw, 0)
    except ValueError as exc:
        raise ValueError(f"Enter a flash address, such as 0x10000. {raw!r} is not a number.") from exc
    if value < 0:
        raise ValueError("Flash address must be zero or positive.")
    return value


def load_flash_manifest(path):
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            "That file is not a valid flash manifest. Choose the build's flasher_args.json."
        ) from exc
    files = data.get("flash_files") if isinstance(data, dict) else None
    if not isinstance(files, dict) or not files:
        raise ValueError("That manifest has no flash_files map. Choose the build's flasher_args.json.")
    parts = []
    seen = set()
    for offset, name in files.items():
        try:
            address = parse_address(str(offset))
        except ValueError as exc:
            raise ValueError(f"Manifest address {offset!r} is not valid.") from exc
        if address in seen:
            raise ValueError(f"The manifest lists {hex(address)} more than once.")
        seen.add(address)
        candidate = Path(str(name))
        if not candidate.is_absolute():
            candidate = path.parent / candidate
        if not candidate.is_file():
            raise ValueError(
                f"Manifest file is missing: {candidate}. Build the project, then import the manifest again."
            )
        parts.append((address, candidate))
    parts.sort(key=lambda item: item[0])
    return parts


def check_merged_image(path, flash_size=None):
    path = Path(path)
    size = path.stat().st_size
    if size < 0x10001:
        raise ValueError(MERGED_ERROR)
    if flash_size is not None and size > flash_size:
        raise ValueError(
            f"The merged image is {format_size(size)}, larger than the device flash "
            f"({format_size(flash_size)})."
        )
    if size > MAX_FLASH:
        raise ValueError(MERGED_ERROR)
    with path.open("rb") as image:
        bootloader = image.read(1)
        image.seek(0x8000)
        partition_table = image.read(2)
        image.seek(0x10000)
        application = image.read(1)
    if (bootloader, partition_table, application) != (b"\xe9", b"\xaa\x50", b"\xe9"):
        raise ValueError(MERGED_ERROR)


def check_backup(path, flash_size):
    if not isinstance(flash_size, int) or flash_size <= 0:
        raise ValueError("Detect the device flash size before restoring.")
    size = Path(path).stat().st_size
    if size != flash_size:
        raise ValueError(
            f"Restore failed. This backup is {format_size(size)} ({size} bytes), but the connected "
            f"device reports {format_size(flash_size)} ({flash_size} bytes). "
            "Choose a full backup taken from this device."
        )


def explain_tool_error(action, output, returncode):
    lowered = output.lower()
    if "no module named esptool" in lowered:
        return "esptool is not installed for this Python. Run: python -m pip install esptool"
    if "no module named platformio" in lowered or "is not recognized as an internal" in lowered:
        return (
            "PlatformIO was not found. Set the PlatformIO executable path to platformio.exe and try again."
        )
    if any(phrase in lowered for phrase in _BUSY_PORT):
        return (
            f"{action} failed because the serial port is in use. "
            "Disconnect the serial monitor and close any other program using that port."
        )
    if "failed to connect" in lowered or "no serial data received" in lowered:
        return (
            f"{action} failed to connect to the board. Check the COM port and chip selection. "
            "Hold the boot button if the chip does not enter download mode, then try again."
        )
    if action == "Build":
        return "Build failed. See the log for the PlatformIO error."
    if action == "Upload":
        return (
            "Upload failed. See the log. Check the COM port, the environment, and that the board is connected."
        )
    return f"{action} failed (exit code {returncode}). See the log."


def line_ending_bytes(name):
    try:
        return ENDINGS[name]
    except KeyError as exc:
        raise ValueError("Choose a line ending: None, LF, CR, or CRLF.") from exc


def normalize_port(value):
    port = str(value).split("—", 1)[0].strip().upper()
    if not (port.startswith("COM") and port[3:].isdecimal()):
        raise ValueError("Select a Windows serial port such as COM32.")
    return port


def platformio_candidates():
    scripts = Path.home() / ".platformio" / "penv" / "Scripts"
    found = [scripts / "platformio.exe", scripts / "pio.exe"]
    for name in ("platformio", "pio"):
        located = shutil.which(name)
        if located:
            found.append(Path(located))
    return found


def find_platformio(explicit=None):
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return [str(path)]
        raise FileNotFoundError(
            f"PlatformIO was not found at {explicit}. Browse to platformio.exe. "
            "A typical path is %USERPROFILE%\\.platformio\\penv\\Scripts\\platformio.exe."
        )
    for candidate in platformio_candidates():
        if candidate.is_file():
            return [str(candidate)]
    if importlib.util.find_spec("platformio") is not None:
        return [sys.executable, "-m", "platformio"]
    raise FileNotFoundError(
        "PlatformIO was not found. Install it, or set the executable path. "
        "A typical path is %USERPROFILE%\\.platformio\\penv\\Scripts\\platformio.exe."
    )


def esptool_available():
    return importlib.util.find_spec("esptool") is not None


def backup_destination(folder):
    return Path(folder) / BACKUP_NAME


def format_size(size):
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.2f} MiB"
    if size >= 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size} B"


def serial_ports():
    try:
        from serial.tools import list_ports
    except ImportError as exc:
        raise RuntimeError("Install esptool to list serial ports. Run: python -m pip install esptool") from exc
    try:
        infos = list(list_ports.comports())
    except OSError as exc:
        raise RuntimeError(f"Could not list serial ports. {exc}") from exc

    def sort_key(info):
        number = info.device[3:] if info.device.upper().startswith("COM") else ""
        return (0, int(number)) if number.isdecimal() else (1, info.device.upper())

    found = []
    for info in sorted(infos, key=sort_key):
        device = info.device.upper()
        description = (info.description or "").strip()
        suffix = f"({info.device})"
        if description.endswith(suffix):
            description = description[: -len(suffix)].strip()
        if description and description.upper() not in {device, "N/A", "UNKNOWN"}:
            label = f"{device} — {description}"
        else:
            label = device
        found.append((device, label))
    return found


def port_rank(label):
    upper = label.upper()
    if "BLUETOOTH" in upper:
        return 2
    if any(hint in upper for hint in ("CP210", "CH340", "CH910", "FTDI", "USB", "UART", "JTAG")):
        return 0
    return 1


def bin_files(folder):
    files = [path for path in Path(folder).iterdir() if path.is_file() and path.suffix.lower() == ".bin"]
    return sorted(files, key=lambda path: path.name.lower())


def stream_process(command, on_line):
    try:
        process = subprocess.Popen(
            command,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise RuntimeError(f"Could not start {command[0]}. {exc}") from exc
    with process:
        for line in process.stdout:
            on_line(line)
        return process.wait()


def open_serial(port, baud):
    try:
        import serial
    except ImportError as exc:
        raise RuntimeError("pyserial is not installed. Run: python -m pip install esptool") from exc
    try:
        return serial.Serial(port, baudrate=int(baud), timeout=0.2)
    except serial.SerialException as exc:
        raise RuntimeError(
            f"Could not open {port}. Close the serial monitor and any other program using that port. {exc}"
        ) from exc


def settings_path():
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "BenchFlasher" / "settings.json"


def load_settings():
    try:
        return json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def save_settings(data):
    try:
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        return


class Monitor:
    """Serial console whose port can be released for esptool or PlatformIO."""

    def __init__(self, events, open_port=open_serial):
        self.events = events
        self.open_port = open_port
        self.stop = threading.Event()
        self.outgoing = queue.Queue()
        self.thread = None
        self.settings = None
        self.active = False
        self._serial = None
        self._retry_delay = 0.5

    def connect(self, port, baud, ending, retries=1, retry_delay=0.5):
        if ending not in ENDINGS:
            raise ValueError("Choose a line ending: None, LF, CR, or CRLF.")
        if self.thread and self.thread.is_alive():
            raise RuntimeError(
                "The serial monitor is still using the serial port. Disconnect it and try again."
            )
        self.settings = (normalize_port(port), int(baud), ending)
        self._retry_delay = retry_delay
        self.stop.clear()
        while not self.outgoing.empty():
            try:
                self.outgoing.get_nowait()
            except queue.Empty:
                break
        self.thread = threading.Thread(
            target=self._run,
            args=(self.settings[0], self.settings[1], retries),
            daemon=True,
        )
        self.thread.start()

    def disconnect(self):
        self.stop.set()
        serial_port = self._serial
        if serial_port is not None:
            try:
                serial_port.close()
            except Exception:
                pass
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
        self.thread = None
        self.active = False
        self._serial = None

    def suspend(self):
        if self.thread is None and not self.active:
            return None
        settings = self.settings if self.active else None
        self.disconnect()
        return settings

    def send(self, text, ending):
        if not self.active:
            raise RuntimeError("Connect the serial monitor before sending a command.")
        self.outgoing.put(text.encode("utf-8", "replace") + line_ending_bytes(ending))

    def _run(self, port, baud, retries):
        serial_port = None
        last_error = None
        for _attempt in range(max(1, retries)):
            if self.stop.is_set():
                return
            try:
                serial_port = self.open_port(port, baud)
                break
            except Exception as exc:
                last_error = exc
                serial_port = None
                if self.stop.wait(self._retry_delay):
                    return
        if serial_port is None:
            self.events.put(("monitor-failed", str(last_error)))
            return
        if self.stop.is_set():
            try:
                serial_port.close()
            except Exception:
                pass
            return
        self._serial = serial_port
        self.active = True
        self.events.put(("monitor-connected", f"Monitor connected on {port} at {baud}."))
        try:
            while not self.stop.is_set():
                try:
                    payload = self.outgoing.get_nowait()
                except queue.Empty:
                    payload = None
                if payload is not None:
                    serial_port.write(payload)
                data = serial_port.readline()
                if data:
                    self.events.put(("monitor", data.decode("utf-8", "replace")))
        except Exception as exc:
            if not self.stop.is_set():
                self.events.put((
                    "monitor-error",
                    f"The serial monitor lost {port}. Close any other program using it and connect again. {exc}",
                ))
        finally:
            self.active = False
            self._serial = None
            try:
                serial_port.close()
            except Exception:
                pass


class PortSession:
    """Lets one owner at a time hold the serial port."""

    def __init__(self, monitor):
        self.monitor = monitor
        self.owner = None
        self.manual_failure = True

    def connect_monitor(self, port, baud, ending, retries=1, retry_delay=0.5, manual=True):
        if self.owner == "operation":
            raise RuntimeError(
                "An upload or flash is using the serial port. Wait for it to finish, then connect."
            )
        if self.owner == "monitor" and self.monitor.active:
            raise RuntimeError("The serial monitor is already connected. Disconnect it before connecting again.")
        self.manual_failure = manual
        self.monitor.connect(port, baud, ending, retries=retries, retry_delay=retry_delay)
        self.owner = "monitor"

    def disconnect_monitor(self):
        self.monitor.disconnect()
        if self.owner == "monitor":
            self.owner = None

    def monitor_closed(self):
        if self.owner == "monitor":
            self.owner = None

    def acquire(self):
        if self.owner == "operation":
            raise RuntimeError("Wait for the current operation to finish.")
        resume = None
        if self.owner == "monitor" or self.monitor.active:
            resume = self.monitor.suspend()
            if self.owner == "monitor":
                self.owner = None
        self.owner = "operation"
        return resume

    def release(self, resume):
        if self.owner == "operation":
            self.owner = None
        if not resume:
            return
        try:
            self.connect_monitor(*resume, retries=10, manual=False)
        except RuntimeError as exc:
            self.monitor.events.put(("monitor-failed", f"Could not reconnect the serial monitor. {exc}"))


APP_BG = "#f3f6f9"
HEADER_BG = "#16324f"
HEADER_MUTED = "#b7c9dc"
TEXT = "#1c2430"
MUTED = "#5c6b7a"
CONSOLE_BG = "#15202b"
CONSOLE_FG = "#d6dee8"


def apply_theme(root):
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=APP_BG, foreground=TEXT, font=("Segoe UI", 10))
    style.configure("TFrame", background=APP_BG)
    style.configure("TLabel", background=APP_BG, foreground=TEXT, font=("Segoe UI", 10))
    style.configure("Hint.TLabel", background=APP_BG, foreground=MUTED, font=("Segoe UI", 9))
    style.configure("TNotebook", background=APP_BG, borderwidth=0, tabmargins=(2, 6, 2, 0))
    style.configure(
        "TNotebook.Tab",
        background="#e1e8f0",
        foreground=TEXT,
        padding=(16, 8),
        font=("Segoe UI", 10),
        borderwidth=0,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", "#ffffff"), ("active", "#edf2f7")],
        foreground=[("selected", HEADER_BG)],
    )
    style.configure(
        "TButton",
        background="#ffffff",
        foreground=TEXT,
        bordercolor="#c5d0dc",
        lightcolor="#ffffff",
        darkcolor="#c5d0dc",
        padding=(12, 6),
        font=("Segoe UI", 10),
    )
    style.map(
        "TButton",
        background=[("pressed", "#e6edf5"), ("active", "#f7f9fb"), ("disabled", "#eef2f6")],
        foreground=[("disabled", "#8b98a6")],
    )
    style.configure(
        "Primary.TButton",
        background=HEADER_BG,
        foreground="#ffffff",
        bordercolor=HEADER_BG,
        lightcolor=HEADER_BG,
        darkcolor=HEADER_BG,
        padding=(14, 7),
        font=("Segoe UI", 10, "bold"),
    )
    style.map(
        "Primary.TButton",
        background=[("pressed", "#0f2438"), ("active", "#24527f"), ("disabled", "#b7c3d0")],
        foreground=[("disabled", "#f7f9fb")],
        bordercolor=[("disabled", "#b7c3d0"), ("active", "#24527f")],
    )
    style.configure(
        "Danger.TButton",
        background="#9b2c2c",
        foreground="#ffffff",
        bordercolor="#9b2c2c",
        lightcolor="#9b2c2c",
        darkcolor="#9b2c2c",
        padding=(12, 6),
        font=("Segoe UI", 10, "bold"),
    )
    style.map(
        "Danger.TButton",
        background=[("pressed", "#6f1d1d"), ("active", "#b42323"), ("disabled", "#e4cfcf")],
        foreground=[("disabled", "#f7f9fb")],
        bordercolor=[("disabled", "#e4cfcf"), ("active", "#b42323")],
    )
    style.configure("TEntry", fieldbackground="#ffffff", bordercolor="#c5d0dc", padding=5)
    style.configure("TCombobox", fieldbackground="#ffffff", bordercolor="#c5d0dc", padding=4)
    style.configure(
        "Treeview",
        background="#ffffff",
        fieldbackground="#ffffff",
        foreground=TEXT,
        rowheight=26,
        bordercolor="#d5dee8",
        font=("Segoe UI", 10),
    )
    style.configure(
        "Treeview.Heading",
        background="#e6edf5",
        foreground=HEADER_BG,
        font=("Segoe UI", 10, "bold"),
        relief="flat",
    )
    style.map("Treeview", background=[("selected", "#d9e6f5")], foreground=[("selected", TEXT)])
    style.configure("TRadiobutton", background=APP_BG, font=("Segoe UI", 10))
    style.configure("TScrollbar", background="#d5dee8", troughcolor=APP_BG, borderwidth=0)
    root.configure(background=APP_BG)


def style_console(widget):
    widget.configure(
        background=CONSOLE_BG,
        foreground=CONSOLE_FG,
        insertbackground=CONSOLE_FG,
        font=("Consolas", 10),
        relief="flat",
        borderwidth=0,
        padx=10,
        pady=8,
        highlightthickness=1,
        highlightbackground="#d5dee8",
        highlightcolor="#24527f",
        selectbackground="#24527f",
        selectforeground="#ffffff",
    )


class Flasher:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.busy = False
        self.ports = []
        self.part_rows = []
        self.saved_port = ""
        self.last_image_dir = None
        self._resume = None
        self.session = PortSession(Monitor(self.events))
        self.readonly_widgets = []
        self.normal_widgets = []
        apply_theme(root)
        root.title("Bench Flasher")
        root.geometry("1000x920")
        root.minsize(900, 800)
        header = tk.Frame(root, bg=HEADER_BG, padx=18, pady=12)
        header.pack(fill="x")
        tk.Label(
            header,
            text="Bench Flasher",
            bg=HEADER_BG,
            fg="#ffffff",
            font=("Segoe UI", 18, "bold"),
        ).pack(anchor="w")
        tk.Label(
            header,
            text="Build, flash, and monitor an ESP bench controller",
            bg=HEADER_BG,
            fg=HEADER_MUTED,
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(2, 0))
        outer = ttk.Frame(root, padding=(16, 12, 16, 8))
        outer.pack(fill="both", expand=True)
        self._build_device_bar(outer)
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="x", pady=(10, 8))
        self.notebook.bind("<<NotebookTabChanged>>", self.fit_notebook)
        self._build_build_tab()
        self._build_flash_tab()
        self._build_monitor_tab()
        ttk.Label(outer, text="Tool output", style="Hint.TLabel").pack(anchor="w", pady=(0, 4))
        log_frame = ttk.Frame(outer)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, state="disabled", wrap="word", height=8)
        style_console(self.log)
        log_scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")
        status_bar = tk.Frame(root, bg="#e6edf5")
        status_bar.pack(fill="x")
        self.status = tk.Label(
            status_bar,
            text="Select the bench controller port. The monitor releases it during upload, flash, erase, backup, and restore.",
            bg="#e6edf5",
            fg=TEXT,
            anchor="w",
            justify="left",
            font=("Segoe UI", 9),
            padx=16,
            pady=8,
            wraplength=920,
        )
        self.status.pack(fill="x")
        self._apply_settings(load_settings())
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(10, self.fit_notebook)
        root.after(50, self.refresh_ports)
        root.after(50, lambda: self.locate_platformio(silent=True))
        root.after(75, self.poll)

    def fit_notebook(self, _event=None):
        selected = self.notebook.select()
        if not selected:
            return
        tab = self.notebook.nametowidget(selected)
        tab.update_idletasks()
        wanted = max(tab.winfo_reqheight() + 28, 220)
        top = self.notebook.winfo_rooty() - self.root.winfo_rooty()
        room = self.root.winfo_height() - top - 180
        if room > 260:
            wanted = min(wanted, room)
        self.notebook.configure(height=wanted)

    def _remember(self, widget, readonly=False):
        if readonly:
            self.readonly_widgets.append(widget)
        else:
            self.normal_widgets.append(widget)
        return widget

    def _build_device_bar(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill="x")
        bar.columnconfigure(1, weight=1)
        ttk.Label(bar, text="Serial port").grid(row=0, column=0, sticky="w")
        self.port = self._remember(ttk.Combobox(bar, state="readonly", width=46), readonly=True)
        self.port.grid(row=0, column=1, sticky="ew", padx=8)
        self.refresh_button = self._remember(ttk.Button(bar, text="Refresh", command=self.refresh_ports))
        self.refresh_button.grid(row=0, column=2, sticky="e")
        ttk.Label(bar, text="Chip").grid(row=0, column=3, sticky="w", padx=(16, 0))
        self.chip = self._remember(ttk.Combobox(bar, state="readonly", width=12, values=CHIPS), readonly=True)
        self.chip.set("auto")
        self.chip.grid(row=0, column=4, sticky="e", padx=(8, 0))

    def _build_build_tab(self):
        tab = ttk.Frame(self.notebook, padding=12)
        tab.columnconfigure(1, weight=1)
        self.notebook.add(tab, text="Build & Upload")
        ttk.Label(tab, text="Project").grid(row=0, column=0, sticky="w", pady=4)
        self.project_path = self._remember(ttk.Entry(tab))
        self.project_path.grid(row=0, column=1, sticky="ew", padx=8, pady=4)
        self.project_button = self._remember(
            ttk.Button(tab, text="Browse", command=self.browse_project)
        )
        self.project_button.grid(row=0, column=2, sticky="e", pady=4)
        ttk.Label(tab, text="Environment").grid(row=1, column=0, sticky="w", pady=4)
        self.environment = self._remember(ttk.Combobox(tab, state="readonly"), readonly=True)
        self.environment.grid(row=1, column=1, sticky="ew", padx=8, pady=4)
        ttk.Label(tab, text="PlatformIO").grid(row=2, column=0, sticky="w", pady=4)
        self.pio_path = self._remember(ttk.Entry(tab))
        self.pio_path.grid(row=2, column=1, sticky="ew", padx=8, pady=4)
        self.pio_button = self._remember(ttk.Button(tab, text="Browse", command=self.browse_pio))
        self.pio_button.grid(row=2, column=2, sticky="e", pady=4)
        self.pio_hint = ttk.Label(
            tab, text="Looking for PlatformIO…", style="Hint.TLabel", wraplength=760, justify="left"
        )
        self.pio_hint.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 8))
        actions = ttk.Frame(tab)
        actions.grid(row=4, column=0, columnspan=3, sticky="w")
        self.build_button = self._remember(ttk.Button(actions, text="Build", command=self.start_build))
        self.build_button.pack(side="left")
        self.upload_button = self._remember(
            ttk.Button(actions, text="Build & Upload", style="Primary.TButton", command=self.start_upload)
        )
        self.upload_button.pack(side="left", padx=(8, 0))
        ttk.Label(
            tab,
            text=(
                "Browse to the cold-room or drying-room project and select its environment. "
                "Build & Upload uses the serial port above, so both projects can be tested on this bench controller. "
                "PlatformIO output appears in Tool output."
            ),
            style="Hint.TLabel",
            wraplength=860,
            justify="left",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(12, 0))

    def _build_flash_tab(self):
        tab = ttk.Frame(self.notebook, padding=12)
        tab.columnconfigure(1, weight=1)
        self.notebook.add(tab, text="Flash from Files")
        self.flash_mode = tk.StringVar(value="merged")
        modes = ttk.Frame(tab)
        modes.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.merged_mode = self._remember(
            ttk.Radiobutton(modes, text="Merged image at 0x0", value="merged", variable=self.flash_mode)
        )
        self.merged_mode.pack(side="left")
        self.parts_mode = self._remember(
            ttk.Radiobutton(modes, text="Separate files", value="parts", variable=self.flash_mode)
        )
        self.parts_mode.pack(side="left", padx=(12, 0))
        ttk.Label(tab, text="Merged image").grid(row=1, column=0, sticky="w", pady=4)
        self.image_path = self._remember(ttk.Entry(tab))
        self.image_path.grid(row=1, column=1, sticky="ew", padx=8, pady=4)
        image_buttons = ttk.Frame(tab)
        image_buttons.grid(row=1, column=2, sticky="e")
        self.load_button = self._remember(ttk.Button(image_buttons, text="Load file", command=self.load_file))
        self.load_button.pack(side="left")
        self.folder_button = self._remember(
            ttk.Button(image_buttons, text="Open folder", command=self.open_folder)
        )
        self.folder_button.pack(side="left", padx=(4, 0))
        files = ttk.Frame(tab)
        files.grid(row=2, column=0, columnspan=3, sticky="nsew", pady=(8, 4))
        files.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        self.files = ttk.Treeview(files, columns=("address", "file"), show="headings", height=5)
        self.files.heading("address", text="Address")
        self.files.heading("file", text="File")
        self.files.column("address", width=110, stretch=False)
        self.files.column("file", width=640, stretch=True)
        file_scroll = ttk.Scrollbar(files, command=self.files.yview)
        self.files.configure(yscrollcommand=file_scroll.set)
        self.files.grid(row=0, column=0, sticky="nsew")
        file_scroll.grid(row=0, column=1, sticky="ns")
        file_buttons = ttk.Frame(tab)
        file_buttons.grid(row=3, column=0, columnspan=3, sticky="w")
        self.add_button = self._remember(ttk.Button(file_buttons, text="Add file", command=self.add_flash_file))
        self.add_button.pack(side="left")
        self.remove_button = self._remember(ttk.Button(file_buttons, text="Remove", command=self.remove_flash_file))
        self.remove_button.pack(side="left", padx=(4, 0))
        self.import_button = self._remember(
            ttk.Button(file_buttons, text="Import manifest", command=self.import_manifest)
        )
        self.import_button.pack(side="left", padx=(4, 0))
        ttk.Label(tab, text="Flash size").grid(row=4, column=0, sticky="w", pady=4)
        size_row = ttk.Frame(tab)
        size_row.grid(row=4, column=1, columnspan=2, sticky="ew", padx=8, pady=4)
        self.flash_size_value = ttk.Label(size_row, text="Not detected")
        self.flash_size_value.pack(side="left")
        self.detect_button = self._remember(ttk.Button(size_row, text="Detect", command=self.detect_flash))
        self.detect_button.pack(side="left", padx=(12, 0))
        ttk.Label(tab, text="Backup folder").grid(row=5, column=0, sticky="w", pady=4)
        self.backup_folder = self._remember(ttk.Entry(tab))
        self.backup_folder.grid(row=5, column=1, sticky="ew", padx=8, pady=4)
        backup_buttons = ttk.Frame(tab)
        backup_buttons.grid(row=5, column=2, sticky="e")
        self.backup_dir_button = self._remember(
            ttk.Button(backup_buttons, text="Select folder", command=self.select_backup_folder)
        )
        self.backup_dir_button.pack(side="left")
        self.backup_button = self._remember(ttk.Button(backup_buttons, text="Backup", command=self.start_backup))
        self.backup_button.pack(side="left", padx=(4, 0))
        ttk.Label(tab, text="Restore file").grid(row=6, column=0, sticky="w", pady=4)
        self.restore_file = self._remember(ttk.Entry(tab))
        self.restore_file.grid(row=6, column=1, sticky="ew", padx=8, pady=4)
        self.restore_load_button = self._remember(
            ttk.Button(tab, text="Load backup", command=self.load_backup)
        )
        self.restore_load_button.grid(row=6, column=2, sticky="e")
        actions = ttk.Frame(tab)
        actions.grid(row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.restore_button = self._remember(ttk.Button(actions, text="Restore", command=self.start_restore))
        self.restore_button.pack(side="left")
        self.erase_button = self._remember(
            ttk.Button(actions, text="Erase", style="Danger.TButton", command=self.start_erase)
        )
        self.erase_button.pack(side="left", padx=(4, 0))
        self.flash_button = self._remember(
            ttk.Button(actions, text="Flash", style="Primary.TButton", command=self.start_flash)
        )
        self.flash_button.pack(side="left", padx=(4, 0))
        ttk.Label(
            tab,
            text=(
                "A merged image is checked, then written at 0x0. Separate files are written only at the "
                "addresses shown above. Backup is optional. Erase is a separate full-chip erase. "
                "Backup and restore use the flash size reported by the connected device."
            ),
            style="Hint.TLabel",
            wraplength=860,
            justify="left",
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(12, 0))

    def _build_monitor_tab(self):
        tab = ttk.Frame(self.notebook, padding=12)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(1, weight=1)
        self.notebook.add(tab, text="Serial Monitor")
        controls = ttk.Frame(tab)
        controls.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(controls, text="Baud").pack(side="left")
        self.baud = self._remember(
            ttk.Combobox(controls, state="readonly", width=10, values=BAUD_RATES),
            readonly=True,
        )
        self.baud.set("115200")
        self.baud.pack(side="left", padx=(8, 12))
        ttk.Label(controls, text="Line ending").pack(side="left")
        self.ending = self._remember(
            ttk.Combobox(controls, state="readonly", width=8, values=tuple(ENDINGS)),
            readonly=True,
        )
        self.ending.set("LF")
        self.ending.pack(side="left", padx=(8, 12))
        self.connect_button = self._remember(
            ttk.Button(controls, text="Connect", style="Primary.TButton", command=self.connect_clicked)
        )
        self.connect_button.pack(side="left")
        self.disconnect_button = self._remember(
            ttk.Button(controls, text="Disconnect", command=self.disconnect_clicked)
        )
        self.disconnect_button.pack(side="left", padx=(4, 12))
        self.clear_button = self._remember(ttk.Button(controls, text="Clear", command=self.clear_monitor))
        self.clear_button.pack(side="left")
        self.save_log_button = self._remember(ttk.Button(controls, text="Save log", command=self.save_monitor))
        self.save_log_button.pack(side="left", padx=(4, 0))
        monitor_frame = ttk.Frame(tab)
        monitor_frame.grid(row=1, column=0, columnspan=2, sticky="nsew")
        monitor_frame.rowconfigure(0, weight=1)
        monitor_frame.columnconfigure(0, weight=1)
        self.monitor_log = tk.Text(monitor_frame, state="disabled", wrap="word", height=10)
        style_console(self.monitor_log)
        monitor_scroll = ttk.Scrollbar(monitor_frame, command=self.monitor_log.yview)
        self.monitor_log.configure(yscrollcommand=monitor_scroll.set)
        self.monitor_log.grid(row=0, column=0, sticky="nsew")
        monitor_scroll.grid(row=0, column=1, sticky="ns")
        send_row = ttk.Frame(tab)
        send_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        send_row.columnconfigure(0, weight=1)
        self.command = self._remember(ttk.Entry(send_row))
        self.command.grid(row=0, column=0, sticky="ew")
        self.command.bind("<Return>", lambda _event: self.send_command())
        self.send_button = self._remember(
            ttk.Button(send_row, text="Send", style="Primary.TButton", command=self.send_command)
        )
        self.send_button.grid(row=0, column=1, padx=(8, 0))

    def _apply_settings(self, data):
        self.saved_port = str(data.get("port") or "")
        chip = str(data.get("chip") or "auto")
        if chip in CHIPS:
            self.chip.set(chip)
        baud = str(data.get("baud") or "115200")
        if baud in BAUD_RATES:
            self.baud.set(baud)
        ending = str(data.get("ending") or "LF")
        if ending in ENDINGS:
            self.ending.set(ending)
        for widget, key in (
            (self.pio_path, "pio"),
            (self.project_path, "project"),
            (self.backup_folder, "backup_folder"),
            (self.restore_file, "restore_file"),
            (self.image_path, "image"),
        ):
            value = str(data.get(key) or "")
            if value:
                widget.insert(0, value)
        project = self.project_path.get().strip()
        if project and (Path(project) / "platformio.ini").is_file():
            try:
                envs = project_environments(project)
            except (OSError, ValueError):
                envs = []
            if envs:
                self.environment.configure(values=envs)
                saved_env = str(data.get("env") or "")
                self.environment.set(saved_env if saved_env in envs else envs[0])

    def current_settings(self):
        return {
            "port": self.selected_port(),
            "chip": self.chip.get(),
            "baud": self.baud.get(),
            "ending": self.ending.get(),
            "pio": self.pio_path.get().strip(),
            "project": self.project_path.get().strip(),
            "env": self.environment.get().strip(),
            "backup_folder": self.backup_folder.get().strip(),
            "restore_file": self.restore_file.get().strip(),
            "image": self.image_path.get().strip(),
        }

    def set_busy(self, busy):
        self.busy = busy
        readonly = "disabled" if busy else "readonly"
        normal = "disabled" if busy else "normal"
        for widget in self.readonly_widgets:
            widget.configure(state=readonly)
        for widget in self.normal_widgets:
            widget.configure(state=normal)

    def close(self):
        if self.busy:
            messagebox.showinfo(
                "Operation in progress",
                "Wait for the operation to finish.",
                parent=self.root,
            )
            return
        self.session.disconnect_monitor()
        save_settings(self.current_settings())
        self.root.destroy()

    def append_text(self, widget, text, limit=200_000):
        widget.configure(state="normal")
        widget.insert("end", text)
        try:
            counted = widget.count("1.0", "end-1c", "chars")
            total = counted[0] if isinstance(counted, tuple) else counted
        except tk.TclError:
            total = None
        if total and total > limit:
            widget.delete("1.0", f"1.0 + {total - limit} chars")
        widget.see("end")
        widget.configure(state="disabled")

    def append_log(self, text):
        self.append_text(self.log, text)

    def append_monitor(self, text):
        self.append_text(self.monitor_log, text)

    def poll(self):
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.append_log(payload)
            elif kind == "monitor":
                self.append_monitor(payload)
            elif kind == "monitor-connected":
                self.append_monitor(payload + "\n")
                self.status.configure(text=payload)
            elif kind == "monitor-failed":
                self.session.monitor_closed()
                self.append_log(payload + "\n")
                self.status.configure(text=payload)
                if self.session.manual_failure and not self.busy:
                    messagebox.showerror("Serial monitor", payload, parent=self.root)
            elif kind == "monitor-error":
                self.append_monitor(f"\n{payload}\n")
                self.status.configure(text=payload)
                self.session.disconnect_monitor()
            elif kind == "flash-size":
                self.flash_size_value.configure(text=f"{format_size(payload)} ({payload} bytes)")
            elif kind == "done":
                self.append_log(payload + "\n")
                self.status.configure(text=payload)
                resume = self._resume
                self._resume = None
                self.set_busy(False)
                self.session.release(resume)
        try:
            self.root.after(75, self.poll)
        except tk.TclError:
            return

    def begin(self, title, header, work, uses_port):
        if self.busy:
            messagebox.showinfo(
                "Busy",
                "Wait for the current operation to finish.",
                parent=self.root,
            )
            return
        resume = None
        if uses_port:
            try:
                resume = self.session.acquire()
            except RuntimeError as exc:
                messagebox.showerror("Serial port", str(exc), parent=self.root)
                return
        self._resume = resume
        self.append_log(header)
        if resume:
            self.append_log(f"Released {resume[0]} so {title} can use the port.\n")
        self.set_busy(True)
        self.status.configure(text=f"{title} in progress…")
        threading.Thread(target=self._worker, args=(work,), daemon=True).start()

    def _worker(self, work):
        try:
            message = work()
        except (OSError, RuntimeError, ValueError) as exc:
            message = str(exc)
        self.events.put(("done", message))

    def run_command(self, command):
        lines = []

        def on_line(line):
            lines.append(line)
            self.events.put(("log", line))

        code = stream_process(command, on_line)
        return code, "".join(lines)

    def refresh_ports(self):
        previous = self.selected_port() or self.saved_port
        try:
            self.ports = serial_ports()
        except RuntimeError as exc:
            self.ports = []
            self.port.configure(values=[])
            self.port.set("")
            self.status.configure(text=str(exc))
            return
        labels = [label for _device, label in self.ports]
        self.port.configure(values=labels)
        if not labels:
            self.port.set("")
            self.status.configure(text="No serial ports found. Connect the controller and refresh.")
            return
        current = next((label for device, label in self.ports if device == previous), None)
        self.port.set(current or min(self.ports, key=lambda item: port_rank(item[1]))[1])
        if str(self.status.cget("text")).startswith("No serial ports"):
            self.status.configure(text="Select the bench controller port.")

    def selected_port(self):
        label = self.port.get().strip()
        for device, item in self.ports:
            if item == label:
                return device
        token = label.split("—", 1)[0].strip().upper()
        if token.startswith("COM") and token[3:].isdecimal():
            return token
        return ""

    def require_port(self):
        return normalize_port(self.selected_port())

    def require_chip(self):
        chip = self.chip.get().strip().lower()
        if chip not in CHIPS:
            raise ValueError("Select an ESP chip, or use auto.")
        return chip

    def guard_esptool(self):
        if esptool_available():
            return True
        messagebox.showerror(
            "esptool",
            "esptool is not installed for this Python.\n\nRun: python -m pip install esptool",
            parent=self.root,
        )
        return False

    def target(self):
        port = self.require_port()
        chip = self.require_chip()
        return port, chip

    def locate_platformio(self, silent=True):
        explicit = self.pio_path.get().strip()
        try:
            command = find_platformio(explicit or None)
        except FileNotFoundError as exc:
            self.pio_hint.configure(text=str(exc))
            if not silent:
                messagebox.showerror("PlatformIO", str(exc), parent=self.root)
            return None
        if not explicit and len(command) == 1:
            self.pio_path.insert(0, command[0])
        if len(command) == 1:
            self.pio_hint.configure(text=f"Using {command[0]}")
        else:
            self.pio_hint.configure(text="Using python -m platformio")
        return command

    def browse_project(self):
        current = self.project_path.get().strip()
        folder = filedialog.askdirectory(
            parent=self.root,
            title="PlatformIO project folder",
            initialdir=current or None,
            mustexist=True,
        )
        if not folder:
            return
        try:
            envs = project_environments(folder)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Project", str(exc), parent=self.root)
            return
        self.project_path.delete(0, "end")
        self.project_path.insert(0, folder)
        self.environment.configure(values=envs)
        self.environment.set(envs[0])
        self.status.configure(text=f"Loaded {Path(folder).name}. Environments: {', '.join(envs)}")

    def browse_pio(self):
        name = filedialog.askopenfilename(
            parent=self.root,
            title="PlatformIO executable",
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")],
        )
        if not name:
            return
        self.pio_path.delete(0, "end")
        self.pio_path.insert(0, name)
        self.locate_platformio(silent=True)

    def start_build(self):
        self._start_platformio(upload=False)

    def start_upload(self):
        self._start_platformio(upload=True)

    def _start_platformio(self, upload):
        project = self.project_path.get().strip()
        environment = self.environment.get().strip()
        try:
            executable = self.locate_platformio(silent=False)
            if executable is None:
                return
            if not project or not (Path(project) / "platformio.ini").is_file():
                raise ValueError("Browse to a PlatformIO project folder that contains platformio.ini.")
            if not environment:
                raise ValueError("Select a build environment from platformio.ini.")
            port = self.require_port() if upload else None
        except (OSError, ValueError, FileNotFoundError) as exc:
            title = "PlatformIO" if isinstance(exc, FileNotFoundError) else "Build"
            messagebox.showerror(title, str(exc), parent=self.root)
            return
        label = "Build & Upload" if upload else "Build"
        if upload and not messagebox.askyesno(
            "Build & Upload",
            f"{project}\n{environment}\n{port}\n\n"
            "Upload this environment to the bench controller.\n"
            "This replaces the firmware on the board.\n\nContinue?",
            parent=self.root,
        ):
            return
        command = platformio_command(executable, project, environment, port)
        action = "Upload" if upload else "Build"

        def work():
            code, output = self.run_command(command)
            if code:
                raise RuntimeError(explain_tool_error(action, output, code))
            return f"{label} completed."

        where = f" to {port}" if port else ""
        self.begin(label, f"\n{label} {environment}{where}\n{project}\n", work, uses_port=upload)

    def load_file(self):
        name = filedialog.askopenfilename(
            parent=self.root,
            title="Load merged image",
            initialdir=self.last_image_dir,
            filetypes=[("Binary image", "*.bin"), ("All files", "*.*")],
        )
        if name:
            self.set_merged_image(Path(name))

    def open_folder(self):
        folder = filedialog.askdirectory(
            parent=self.root,
            title="Open a folder and load a file",
            initialdir=self.last_image_dir,
            mustexist=True,
        )
        if not folder:
            return
        self.last_image_dir = folder
        try:
            files = bin_files(folder)
        except OSError as exc:
            messagebox.showerror("Open folder", str(exc), parent=self.root)
            return
        if not files:
            messagebox.showinfo("Open folder", "That folder has no .bin files.", parent=self.root)
            return
        chosen = self.pick_file(folder, files)
        if chosen is not None:
            self.set_merged_image(chosen)

    def pick_file(self, folder, files):
        dialog = tk.Toplevel(self.root)
        dialog.title("Load a file")
        dialog.transient(self.root)
        dialog.geometry("640x360")
        dialog.minsize(420, 240)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)
        ttk.Label(dialog, text=str(folder)).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))
        frame = ttk.Frame(dialog, padding=(12, 0, 12, 0))
        frame.grid(row=1, column=0, sticky="nsew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        listbox = tk.Listbox(frame, activestyle="dotbox", exportselection=False)
        scroll = ttk.Scrollbar(frame, command=listbox.yview)
        listbox.configure(yscrollcommand=scroll.set)
        listbox.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        for path in files:
            listbox.insert("end", f"{path.name}   {format_size(path.stat().st_size)}")
        listbox.selection_set(0)
        listbox.activate(0)
        listbox.focus_set()
        chosen = []

        def accept(_event=None):
            selection = listbox.curselection()
            if not selection:
                return
            chosen.append(files[selection[0]])
            dialog.destroy()

        listbox.bind("<Double-Button-1>", accept)
        dialog.bind("<Return>", accept)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        buttons = ttk.Frame(dialog, padding=12)
        buttons.grid(row=2, column=0, sticky="e")
        ttk.Button(buttons, text="Load", command=accept).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right", padx=(0, 8))
        dialog.grab_set()
        self.root.wait_window(dialog)
        return chosen[0] if chosen else None

    def set_merged_image(self, path):
        try:
            check_merged_image(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Image", str(exc), parent=self.root)
            return
        self.image_path.delete(0, "end")
        self.image_path.insert(0, str(path))
        self.flash_mode.set("merged")
        self.last_image_dir = str(Path(path).parent)
        self.status.configure(text=f"Loaded merged image {Path(path).name} for address 0x0.")

    def add_flash_file(self):
        name = filedialog.askopenfilename(
            parent=self.root,
            title="Add a flash file",
            initialdir=self.last_image_dir,
            filetypes=[("Binary image", "*.bin"), ("All files", "*.*")],
        )
        if not name:
            return
        address_text = simpledialog.askstring(
            "Flash address",
            "Enter the flash address for this file, such as 0x0, 0x8000, or 0x10000.\n"
            "A standalone .bin is not written unless you enter its address.",
            parent=self.root,
        )
        if address_text is None:
            return
        try:
            address = parse_address(address_text)
        except ValueError as exc:
            messagebox.showerror("Flash address", str(exc), parent=self.root)
            return
        if any(existing == address for existing, _path in self.part_rows):
            messagebox.showerror(
                "Flash address",
                f"{hex(address)} is already used. Remove that file or choose another address.",
                parent=self.root,
            )
            return
        path = Path(name)
        self.last_image_dir = str(path.parent)
        self.part_rows.append((address, path))
        self.flash_mode.set("parts")
        self.refresh_parts()
        self.status.configure(text=f"Added {path.name} at {hex(address)}.")

    def remove_flash_file(self):
        selection = self.files.selection()
        if not selection:
            return
        del self.part_rows[self.files.index(selection[0])]
        self.refresh_parts()

    def refresh_parts(self):
        self.part_rows.sort(key=lambda item: item[0])
        children = self.files.get_children()
        if children:
            self.files.delete(*children)
        for address, path in self.part_rows:
            self.files.insert("", "end", values=(hex(address), str(path)))

    def built_manifest(self):
        project = self.project_path.get().strip()
        environment = self.environment.get().strip()
        if not project or not environment:
            return None
        candidate = Path(project) / ".pio" / "build" / environment / "flasher_args.json"
        return candidate if candidate.is_file() else None

    def import_manifest(self):
        path = self.built_manifest()
        if path is None:
            chosen = filedialog.askopenfilename(
                parent=self.root,
                title="Import flash manifest",
                filetypes=[("PlatformIO flash manifest", "flasher_args.json"), ("JSON", "*.json")],
            )
            if not chosen:
                return
            path = Path(chosen)
        try:
            parts = load_flash_manifest(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Manifest", str(exc), parent=self.root)
            return
        self.part_rows = parts
        self.flash_mode.set("parts")
        self.refresh_parts()
        listing = ", ".join(hex(address) for address, _path in parts)
        self.status.configure(text=f"Imported {len(parts)} files from {path.name}: {listing}")

    def select_backup_folder(self):
        current = self.backup_folder.get().strip()
        folder = filedialog.askdirectory(
            parent=self.root,
            title="Select backup folder",
            initialdir=current or None,
            mustexist=True,
        )
        if not folder:
            return
        self.backup_folder.delete(0, "end")
        self.backup_folder.insert(0, folder)
        self.status.configure(text=f"Backup folder: {folder}")

    def load_backup(self):
        current = self.restore_file.get().strip()
        initial = str(Path(current).parent) if current else None
        name = filedialog.askopenfilename(
            parent=self.root,
            title="Load full flash backup",
            initialdir=initial,
            filetypes=[("Binary image", "*.bin"), ("All files", "*.*")],
        )
        if not name:
            return
        self.restore_file.delete(0, "end")
        self.restore_file.insert(0, name)
        self.status.configure(text=f"Restore file: {Path(name).name}")

    def detect_flash(self):
        if not self.guard_esptool():
            return
        try:
            port, chip = self.target()
        except ValueError as exc:
            messagebox.showerror("Serial port", str(exc), parent=self.root)
            return
        command = detect_command(sys.executable, port, chip)

        def work():
            code, output = self.run_command(command)
            if code:
                raise RuntimeError(explain_tool_error("Detect flash", output, code))
            size = parse_flash_size(output)
            self.events.put(("flash-size", size))
            return f"Flash size: {format_size(size)} ({size} bytes)."

        self.begin("Detect flash", f"\nDetect flash on {port} ({chip})\n", work, uses_port=True)

    def start_backup(self):
        if not self.guard_esptool():
            return
        try:
            port, chip = self.target()
        except ValueError as exc:
            messagebox.showerror("Serial port", str(exc), parent=self.root)
            return
        folder = self.backup_folder.get().strip()
        if not folder:
            self.select_backup_folder()
            folder = self.backup_folder.get().strip()
            if not folder:
                return
        if not Path(folder).is_dir():
            messagebox.showerror("Backup folder", "Select a folder for the backup.", parent=self.root)
            return
        dest = backup_destination(folder)
        if dest.exists() and not messagebox.askyesno(
            "Backup",
            f"{dest.name} already exists in that folder.\n\nOverwrite it?",
            parent=self.root,
        ):
            return
        python = sys.executable

        def work():
            code, output = self.run_command(detect_command(python, port, chip))
            if code:
                raise RuntimeError(explain_tool_error("Detect flash", output, code))
            size = parse_flash_size(output)
            self.events.put(("flash-size", size))
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=dest.parent, suffix=".partial", delete=False) as handle:
                    temporary = Path(handle.name)
                code, output = self.run_command(backup_command(python, port, chip, size, temporary))
                if code:
                    raise RuntimeError(explain_tool_error("Backup", output, code))
                actual = temporary.stat().st_size
                if actual != size:
                    raise RuntimeError(
                        f"Backup failed. The read was incomplete ({actual} bytes, expected {size}). "
                        "Check the connection and try again."
                    )
                os.replace(temporary, dest)
                temporary = None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return f"Backup completed ({format_size(size)})."

        self.begin("Backup", f"\nBackup on {port} ({chip}) → {dest}\n", work, uses_port=True)

    def start_restore(self):
        if not self.guard_esptool():
            return
        try:
            port, chip = self.target()
        except ValueError as exc:
            messagebox.showerror("Serial port", str(exc), parent=self.root)
            return
        name = self.restore_file.get().strip()
        path = Path(name) if name else None
        if path is None or not path.is_file():
            messagebox.showerror("Restore", "Load a full flash backup to restore.", parent=self.root)
            return
        if not messagebox.askyesno(
            "Restore",
            f"{port}\n{chip}\n{path}\n\n"
            "Overwrite all internal flash with this backup.\n"
            "The file size is checked against the connected device before anything is written.\n\n"
            "Continue?",
            parent=self.root,
        ):
            return
        python = sys.executable

        def work():
            code, output = self.run_command(detect_command(python, port, chip))
            if code:
                raise RuntimeError(explain_tool_error("Detect flash", output, code))
            size = parse_flash_size(output)
            self.events.put(("flash-size", size))
            check_backup(path, size)
            code, output = self.run_command(restore_command(python, port, chip, path))
            if code:
                raise RuntimeError(explain_tool_error("Restore", output, code))
            return "Restore completed."

        self.begin("Restore", f"\nRestore on {port} ({chip}) · {path}\n", work, uses_port=True)

    def start_erase(self):
        if not self.guard_esptool():
            return
        try:
            port, chip = self.target()
        except ValueError as exc:
            messagebox.showerror("Serial port", str(exc), parent=self.root)
            return
        if not messagebox.askyesno(
            "Erase",
            f"{port}\n{chip}\n\nErase all internal flash, including firmware and saved settings.\n\nContinue?",
            parent=self.root,
        ):
            return
        command = erase_command(sys.executable, port, chip)

        def work():
            code, output = self.run_command(command)
            if code:
                raise RuntimeError(explain_tool_error("Erase", output, code))
            return "Erase completed."

        self.begin("Erase", f"\nErase on {port} ({chip})\n", work, uses_port=True)

    def start_flash(self):
        if not self.guard_esptool():
            return
        try:
            port, chip = self.target()
            if self.flash_mode.get() == "merged":
                name = self.image_path.get().strip()
                path = Path(name) if name else None
                if path is None or not path.is_file():
                    raise ValueError(
                        "Load a merged serial image, or switch to Separate files and set an address for each .bin."
                    )
                command = merged_flash_command(sys.executable, port, chip, path)
                listing = f"0x0  {path}"
            elif self.flash_mode.get() == "parts":
                if not self.part_rows:
                    raise ValueError(
                        "Add .bin files with flash addresses, or import the build manifest. "
                        "A standalone .bin is not written at 0x0 unless you enter that address."
                    )
                command = flash_parts_command(sys.executable, port, chip, self.part_rows)
                listing = "\n".join(
                    f"{hex(address)}  {path}" for address, path in sorted(self.part_rows)
                )
            else:
                raise ValueError("Choose a merged image or separate files.")
        except (OSError, ValueError) as exc:
            messagebox.showerror("Flash", str(exc), parent=self.root)
            return
        if not messagebox.askyesno(
            "Flash",
            f"{port}\n{chip}\n\n{listing}\n\nWrite these files?\n\nContinue?",
            parent=self.root,
        ):
            return

        def work():
            code, output = self.run_command(command)
            if code:
                raise RuntimeError(explain_tool_error("Flash", output, code))
            return "Flash completed."

        self.begin("Flash", f"\nFlash on {port} ({chip})\n{listing}\n", work, uses_port=True)

    def connect_clicked(self):
        if self.busy:
            messagebox.showerror(
                "Serial monitor",
                "Wait for the upload or flash to finish before opening the serial port.",
                parent=self.root,
            )
            return
        try:
            port = self.require_port()
            baud = int(self.baud.get())
            ending = self.ending.get()
            line_ending_bytes(ending)
            self.session.connect_monitor(port, baud, ending, retries=1, manual=True)
        except (RuntimeError, ValueError) as exc:
            messagebox.showerror("Serial monitor", str(exc), parent=self.root)

    def disconnect_clicked(self):
        if self.busy:
            messagebox.showinfo(
                "Busy",
                "Wait for the current operation to finish.",
                parent=self.root,
            )
            return
        self.session.disconnect_monitor()
        self.append_monitor("\nDisconnected.\n")
        self.status.configure(text="Serial monitor disconnected.")

    def clear_monitor(self):
        self.monitor_log.configure(state="normal")
        self.monitor_log.delete("1.0", "end")
        self.monitor_log.configure(state="disabled")

    def save_monitor(self):
        name = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save serial log",
            defaultextension=".txt",
            initialfile="serial-log.txt",
            filetypes=[("Text", "*.txt"), ("All files", "*.*")],
        )
        if not name:
            return
        text = self.monitor_log.get("1.0", "end-1c")
        try:
            Path(name).write_text(text, encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Save log", str(exc), parent=self.root)
            return
        self.status.configure(text=f"Saved serial log to {name}")

    def send_command(self):
        ending = self.ending.get()
        text = self.command.get()
        try:
            self.session.monitor.send(text, ending)
        except (RuntimeError, ValueError) as exc:
            messagebox.showerror("Serial monitor", str(exc), parent=self.root)
            return
        visible = {"None": "", "LF": "\\n", "CR": "\\r", "CRLF": "\\r\\n"}[ending]
        self.append_monitor(f"> {text}{visible}\n")
        self.command.delete(0, "end")


if __name__ == "__main__":
    window = tk.Tk()
    Flasher(window)
    window.mainloop()
