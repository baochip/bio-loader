# BIO Uploader

Use the Python script in this repo to upload a BIO program to a Dabao

## Prerequisites

You'll need **Python 3.9+** and **pipx**.

### Install pipx

**macOS**
```bash
brew install pipx
pipx ensurepath
```

**Windows**
```powershell
pip install pipx
pipx ensurepath
```

**Linux**
```bash
pip install pipx
pipx ensurepath
```

After running `pipx ensurepath`, restart your terminal.

---

## Install

```bash
pipx install git+https://github.com/baochip/bio-loader.git
```

or if cloned locally:

```bash
pipx install /path/to/your/local/repo
```

Windows users will also need to manually `pip install pyreadline3` as that does not ship as a standard library in windows.

---

## Usage

Plug a Dabao into your computer and run the following script:

```bash
bio-loader --port /dev/ttyACM0 --code mycode.bin --quantum 10MHz --pads PC0,PC2
```

The `.bin` file produced by [`bio-sim`](https://github.com/baochip/bio-sim) is directly compatible with this script.


| Flag | Description |
|---|---|
| `--port` | Serial port, e.g. `/dev/ttyACM0` (Linux), `/dev/tty.usbserial-*` (macOS), `COM3` (Windows) |
| `--code` | Path to your code |
| `--pads` | I/Os to map into the BIO (expects a string description of the pad label) |
| `--quantum` | Setting for the quantum divider (accepts a number with MHz (or mhz), KHz (or khz), Hz (or hz), or bare number for Hz). Examples: 350MHz  48KHz  1000  3.5MHz.  Max: 350 MHz.
| `--clear` | Clears the current code, stops the BIO and exits (other arguments are ignored) |
| `--delay` | Delay between chunks in seconds (default: `0.2`) |

Don't know how to find your serial port? see the guide below.

---

## Updates

```bash
pipx install --force git+https://github.com/baochip/bio-loader.git
```

---

# Finding Your Serial Port

The Dabao shows up as a serial device when plugged in via USB. Here's how to find the port name on various OSes.

---

## Linux

Plug in the Dabao, then run:

```bash
ls /dev/ttyACM*
```

It will most likely appear as `/dev/ttyACM0`. If you have multiple devices plugged in you might see `/dev/ttyACM1`, `/dev/ttyACM2`, etc. - try unplugging and replugging to see which one appears and disappears.

You can also get more detail with:

```bash
dmesg | tail -20
```

Look for a line mentioning `ttyACM0` or `cdc_acm` near the bottom.

### Permissions - avoiding sudo

On most Linux distros, serial ports are owned by the `dialout` group (sometimes `uucp`). By default your user may not be in this group, which means you'd need `sudo` to access the port. To fix this permanently:

```bash
sudo usermod -aG dialout $USER
```

Then **log out and log back in** (or reboot) for the change to take effect. You can verify it worked with:

```bash
groups
```

You should see `dialout` in the list. After that, `bio-loader` will work without `sudo`.

> **Note:** On some distros (e.g. Arch) the group is called `uucp` instead of `dialout`. If the `dialout` command has no effect, check the port's actual group with `ls -l /dev/ttyACM0` and substitute that group name in the `usermod` command above.

---

## macOS

macOS does not use `ttyACM` - the device will appear under a different name depending on the USB serial chip it uses. Plug in the Dabao, then run:

```bash
ls /dev/tty.usbmodem* /dev/tty.usbserial* 2>/dev/null
```

You should see something like `/dev/tty.usbmodem101` or `/dev/tty.usbserial-0001`. Use whichever appears.

If nothing shows up, try:

```bash
ls /dev/cu.*
```

And look for anything that wasn't there before you plugged in.

### Permissions

macOS does not require any special group membership - the port should be accessible to your user account without any extra steps.

---

## Windows

1. Plug in the Dabao.
2. Open **Device Manager** - press `Win + X` and select it from the menu, or search for it in the Start menu.
3. Expand the **Ports (COM & LPT)** section.
4. Look for an entry like **USB Serial Device (COM3)** - the `COM` number is what you need.

If nothing appears under Ports, try the **Other devices** section; you may need to install a driver. Check the Dabao documentation for the specific driver if so.

Use the port name exactly as shown, e.g.:

```bash
bio-loader --port COM3 --code mycode.bin --quantum 10MHz --pads PC0,PC2
```

> **Tip:** If you're on Windows and using WSL, the port won't be directly available inside WSL. Either run `bio-loader` from a regular Windows terminal (PowerShell or CMD), or look into [usbipd-win](https://github.com/dorssel/usbipd-win) to forward USB devices into WSL.