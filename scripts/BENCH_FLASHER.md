# Bench Flasher

Windows desktop tool for developing and testing ESP boards on one bench controller. Point it at the cold-room project or the drying-room project, build and upload from PlatformIO, or flash image files directly. The same serial port is used for both projects.

## Setup

1. Install Python 3.11 or newer, including Tkinter. The python.org installer includes Tkinter.
2. Install esptool for that same Python:

   ```powershell
   python -m pip install esptool
   ```

3. Install [PlatformIO](https://platformio.org/) for the Build & Upload tab. The tool looks for `platformio` or `pio` on `PATH`, then for:

   ```text
   %USERPROFILE%\.platformio\penv\Scripts\platformio.exe
   ```

   If none of those exist, paste or browse to `platformio.exe` in the PlatformIO field.
4. Close any other serial monitor before uploading or flashing. This app releases its own monitor automatically.

## Start

From the repository root:

```powershell
python scripts/bench_flasher.py
```

`python flasher.py` starts the same tool.

The serial port and chip at the top are shared. **Refresh** reloads the ports Windows currently exposes. **Chip** can be a specific ESP chip or **auto**, which lets esptool detect it.

## Build & Upload

1. Click **Browse** and choose a PlatformIO project folder (`platformio.ini` must be in that folder).
2. Select the environment loaded from that file.
3. **Build** compiles it and does not open the serial port.
4. **Build & Upload** asks for confirmation, then builds and uploads to the serial port selected above.

To test the other room on the same controller, browse to that project, select its environment, and upload again. Upload always uses the port selected at the top, through PlatformIO `--upload-port`.

Success and failure are reported in the status line. The PlatformIO transcript is in **Tool output**. A failed build says to read that log. A failed upload says to check the COM port, the environment, and the USB connection. If the port is busy, close the other program that has it open.

## Flash from Files

The chip and port at the top are passed to esptool.

**Merged image at 0x0.** Use **Load file** or **Open folder** for a merged serial image such as `*-serial.bin`. The file must contain an ESP bootloader at the start, a partition table at `0x8000`, and an application at `0x10000`. Flash writes that file at `0x0`. A normal application `.bin` is rejected here.

**Separate files.** **Add file** asks for a flash address such as `0x0`, `0x8000`, or `0x10000`. Nothing is written without an address, and a standalone `.bin` is not assumed to belong at `0x0`. **Import manifest** reads the build's `flasher_args.json` from `.pio/build/<environment>/` when that file exists, otherwise it asks you to choose the manifest. The address and path are shown in the list before **Flash**.

**Flash** asks for confirmation and shows every address that will be written. It does not erase the whole chip.

**Erase** is separate. It asks for confirmation, then erases all internal flash, including firmware and saved settings.

**Backup** is optional. Choose a folder, then **Backup**. The tool reads the connected device's flash size with esptool and writes `controller-backup.bin` of that exact size. It does not assume 16 MiB.

**Restore** asks for confirmation, reads the device flash size again, and writes the backup only when the file length matches that size. A backup from a different flash size is refused before anything is written.

**Detect** only queries the flash size. It does not erase or program the chip.

## Serial Monitor

Choose the baud rate (default 115200) and a line ending: **None**, **LF**, **CR**, or **CRLF**. **Connect** opens the selected port. Type a command and press **Send** or Enter. **Clear** empties the monitor view. **Save log** writes it to a text file. **Disconnect** closes the port.

Upload, flash, erase, backup, restore, and flash-size detection need the port. If the monitor is connected, the tool disconnects it first and reconnects with the same port, baud, and line ending after the operation finishes. Only one operation holds the port at a time.

## When something fails

| What you see | What to do |
| --- | --- |
| esptool is not installed for this Python | Run `python -m pip install esptool` with the same Python that launches the app. |
| PlatformIO was not found | Set the PlatformIO field to `platformio.exe`, then try the build again. |
| The serial port is in use | Disconnect this monitor and close any other serial program, then retry. |
| Build failed | Read Tool output. The project or the selected environment did not compile. |
| Upload failed, or the board did not connect | Check the COM port, the chip, and the USB cable. Hold the boot button if the chip does not enter download mode. |
| Restore refused the backup | The file length does not match the flash size of the board that is connected. Take a new backup from this device. |
| A standalone `.bin` was rejected | Add it under Separate files and enter its real flash address, or import `flasher_args.json`. |

## Tests

From the repository root:

```powershell
python -m unittest scripts/test_bench_flasher.py
```

The tests build esptool and PlatformIO commands, check image and backup rules, and exercise the serial handoff with a fake port. They do not erase or flash a connected board.
