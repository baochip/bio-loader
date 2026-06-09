"""
bio_loader.py - Upload a BIO coprocessor program to a device over serial.

Chunk wire format (70 bytes before base64) - identical to send_image.py:
  [0:2]   u16  chunk index (big-endian)
  [2:66]  u8*64 pixel data
  [66:70] u32  CRC-32 over bytes [0:66] (big-endian)

Serial protocol:
  Host   -> "bio <base64>\\n"          chunk upload
  Host   -> "bio pin <n> [n ...]\\n"   set I/O pin list
  Host   -> "bio clk <hz>\\n"          set clock rate in Hz
  Host   -> "bio clear\\n"             wipe stored program
  Device -> "OK"                       chunk accepted, not yet complete
  Device -> "ERR"                      bad base64, wrong length, or CRC mismatch
  Device -> "SUCCESS"                  last chunk received, program active
  Device -> "CLEAR"                    clear acknowledged

--quantum syntax examples
  700MHz  /  700mhz  /  700MHZ  -> 700 000 000 Hz
  350MHz  /  350mhz  /  350MHZ  -> 350 000 000 Hz
  48KHz   /  48khz              ->      48 000 Hz
  1000    /  1000Hz             ->       1 000 Hz   (bare number -> Hz)
  3.5MHz                        ->   3 500 000 Hz
"""

import argparse
import struct
import sys
import time
import zlib
import base64
import re
from pathlib import Path
from typing import Dict, List, Optional

try:
    import serial
except ImportError:
    sys.exit("pyserial is required: pip install pyserial")

# -- Dabao Pad label -> physical pin mapping ------------------------------------

PAD_MAPPING: Dict[int, int] = {
    'PB1': 1,
    'PB2': 2,
    'PB3': 3,
    'PB4': 4,
    'PB5': 5,
    'PB11': 11,
    'PB12': 12,
    # 'PB13': 13, # serial console
    # 'PB14': 14, # serial console
    'PC0': 16,
    'PC1': 17,
    'PC2': 18,
    'PC3': 19,
    'PC7': 23,
    'PC8': 24,
    # 'PC9: 25,  # analog tie
    'PC10': 26,
    'PC11': 27,
    'PC12': 28,
    'PC13': 29,
}

# -- BIO / packing constants --------------------------------------------------

MAX_CODE_BYTES  = 0xF00
CHUNK_DATA_SIZE = 64
NUM_CHUNKS      = MAX_CODE_BYTES // CHUNK_DATA_SIZE  # 64
CHUNK_HDR_SIZE  = 2   # u16 index
CHUNK_CRC_SIZE  = 4   # u32 crc32
CHUNK_WIRE_SIZE = CHUNK_HDR_SIZE + CHUNK_DATA_SIZE + CHUNK_CRC_SIZE  # 70

# -- Serial constants ---------------------------------------------------------

BAUD_RATE      = 1_000_000
SERIAL_TIMEOUT = 3.0   # seconds waiting for a response line
RETRY_DELAY    = 0.5   # seconds between retries on ERR
MAX_RETRIES    = 3

READY_TIMEOUT  = 0.5   # per-readline timeout during the attention phase (seconds)

DEFAULT_LINE_DELAY = 0.2  # seconds between chunks

# How long to sleep between in_waiting polls when the receive buffer is empty.
# Keeps CPU near-zero during idle waits without adding noticeable latency.
_POLL_SLEEP = 0.005

def _readline(ser: serial.Serial, timeout: float) -> bytes:
    """
    readline() replacement that sleeps instead of busy-waiting.

    pyserial's built-in readline() calls read(1) in a tight loop, burning CPU
    while it waits for each byte.  This version checks ser.in_waiting (a fast,
    non-blocking OS buffer query) and sleeps for _POLL_SLEEP between checks,
    keeping CPU usage near zero.  Resets the deadline each time new bytes
    arrive so a slow but continuous stream is not cut short.
    """
    buf = bytearray()
    deadline = time.monotonic() + timeout

    while True:
        if ser.in_waiting:
            chunk = ser.read(ser.in_waiting)
            buf.extend(chunk)
            deadline = time.monotonic() + timeout   # data arrived - extend window
            if b"\n" in buf:
                idx = buf.index(b"\n")
                line = bytes(buf[: idx + 1])
                # push any remainder back into a small local buffer... we can't
                # un-read from pyserial, but in practice the device sends one
                # line at a time, so the remainder is almost always empty.
                return line
        elif time.monotonic() >= deadline:
            return bytes(buf)   # timeout - return whatever we have (may be b"")
        else:
            time.sleep(_POLL_SLEEP)


# -- Argument helpers ---------------------------------------------------------

def parse_quantum(raw: str) -> int:
    """
    Parse a frequency string into an integer Hz value.

    Accepts an optional float mantissa followed by an optional unit suffix
    (MHz / KHz / Hz, case-insensitive).  A bare number is treated as Hz.

    Examples:
        "700MHz"  -> 700_000_000
        "350MHz"  -> 350_000_000
        "3.5MHz"  -> 3_500_000
        "48KHz"   -> 48_000
        "1000"    -> 1_000
        "1000hz"  -> 1_000
    """
    raw = raw.strip()
    m = re.fullmatch(
        r"([0-9]*\.?[0-9]+)\s*(MHz|mhz|MHZ|KHz|khz|KHZ|Hz|hz|HZ)?",
        raw,
        re.IGNORECASE,
    )
    if not m:
        raise argparse.ArgumentTypeError(
            f"Cannot parse frequency '{raw}'. "
            "Expected a number with an optional MHz, KHz, or Hz suffix."
        )

    value = float(m.group(1))
    suffix = (m.group(2) or "Hz").lower()

    if "mhz" in suffix:
        hz = value * 1_000_000
    elif "khz" in suffix:
        hz = value * 1_000
    else:
        hz = value

    if hz <= 0:
        raise argparse.ArgumentTypeError(f"Frequency must be positive, got {raw!r}")
    if hz > 700_000_000:
        raise argparse.ArgumentTypeError(
            f"Frequency {hz:.0f} Hz exceeds the 700 MHz device maximum"
        )

    return int(hz)


def parse_pads(raw: str) -> List[int]:
    """
    Parse a comma- or space-separated list of dabao pads
    and return the corresponding physical pin numbers, deduplicated and sorted.
    """
    tokens = re.split(r"[,\s]+", raw.strip())
    pins: List[int] = []
    for tok in tokens:
        if not tok:
            continue
        if tok.upper() not in PAD_MAPPING:
            raise argparse.ArgumentTypeError(
                f"Dabao pad {tok} is not valid; choose from {sorted(PAD_MAPPING)}"
            )
        pins.append(PAD_MAPPING[tok.upper()])
    pins = sorted(set(pins))
    if not pins:
        raise argparse.ArgumentTypeError("No valid dabao pads provided")
    return pins

# -- Chunk helpers ------------------------------------------------------------

def make_chunk(index: int, data: bytes) -> bytes:
    """Build a 70-byte chunk: u16 index || 64-byte data || u32 crc32."""
    assert len(data) == CHUNK_DATA_SIZE
    header  = struct.pack(">H", index)
    payload = header + data
    crc     = zlib.crc32(payload) & 0xFFFF_FFFF
    return payload + struct.pack(">I", crc)

# -- Serial helpers -----------------------------------------------------------

def open_port(port: str) -> serial.Serial:
    try:
        return serial.Serial(
            port,
            baudrate = BAUD_RATE,
            bytesize = serial.EIGHTBITS,
            parity   = serial.PARITY_NONE,
            stopbits = serial.STOPBITS_ONE,
            timeout  = SERIAL_TIMEOUT,
        )
    except serial.SerialException as e:
        sys.exit(f"[ERROR] Cannot open serial port {port}: {e}")


def readline_skip_echo(ser: serial.Serial) -> str:
    """
    Read a response line, skipping any '[console]' local-echo lines
    """
    raw      = ser.readline()
    response = raw.decode("ascii", errors="replace").strip()
    if response.startswith("[console]"):
        print(f"[DBG]  skipping echo: {response}")
        raw      = ser.readline()
        response = raw.decode("ascii", errors="replace").strip()
    return response


def send_command(
    ser: serial.Serial,
    line: str,
    *,
    ok_responses: tuple[str, ...] = ("OK",),
    label: str = "command",
) -> None:
    """
    Send a single newline-terminated command and wait for a recognized response.

    The device may emit an arbitrary number of debug/log lines before the final
    OK or ERR.  This function consumes and prints all such lines, only acting on
    the first recognized response token:
      - a string in ok_responses  -> return normally
      - "ERR"                     -> retry (up to MAX_RETRIES), then exit
      - b"" from readline()       -> readline timed out (SERIAL_TIMEOUT seconds
                                    of silence); retry, then exit
    """
    line_b = (line if line.endswith("\n") else line + "\n").encode("ascii")

    for attempt in range(MAX_RETRIES + 1):
        ser.write(line_b)
        got_err    = False
        timed_out  = False
        while True:
            raw = ser.readline()
            if not raw:                    # b"" -> SERIAL_TIMEOUT of silence
                timed_out = True
                break
            response = raw.decode("ascii", errors="replace").strip()
            if response.startswith("[console]"):
                continue                   # skip local echo, keep reading
            if response in ok_responses:
                return
            if response == "ERR":
                got_err = True
                break

            if response:                   # debug / log line from the device
                print(f"[DEV]  {response}")
            # empty decoded line -> keep reading
        if got_err:
            if attempt < MAX_RETRIES:
                print(f"[WARN] {label} returned ERR - retry {attempt + 1}/{MAX_RETRIES}")
                time.sleep(RETRY_DELAY)
            else:
                sys.exit(f"[ERROR] {label} failed after {MAX_RETRIES} retries")
        else:                              # timed_out
            if attempt < MAX_RETRIES:
                print(f"[WARN] {label} timed out waiting for response - retry {attempt + 1}/{MAX_RETRIES}")
                time.sleep(RETRY_DELAY)
            else:
                sys.exit(f"[ERROR] {label} - no valid response within {SERIAL_TIMEOUT}s after {MAX_RETRIES} retries")


# -- Top-level operations -----------------------------------------------------
def wait_for_ready(ser: serial.Serial) -> None:
    """
    Drain any stale data out of the host-side receive buffer, then confirm
    the device is listening with a single "bio ready" / "OK" exchange.

    Strategy:
      1. reset_input_buffer() - clears whatever the OS had buffered already.
      2. Drain loop - read lines with a short timeout until readline() returns
         b"" (timeout), meaning the pipe is silent.  This catches anything the
         device was still transmitting when we opened the port.
      3. Send "bio ready" and wait for "OK" with the normal timeout now that
         the buffer is known-clean.
    """
    print("[INFO] Draining stale serial data...")
    ser.reset_input_buffer()

    saved_timeout = ser.timeout
    ser.timeout   = READY_TIMEOUT          # short timeout for the drain phase

    try:
        drained = 0
        while True:
            line = ser.readline()
            if not line:                   # b"" -> readline timed out -> pipe silent
                break
            drained += 1
            decoded = line.decode("ascii", errors="replace").strip()
            if decoded:
                print(f"[DBG]  Drained: {decoded!r}")

        if drained:
            print(f"[INFO] Drained {drained} line(s); buffer clear")
        else:
            print("[INFO] Buffer already empty")

        ser.timeout = saved_timeout        # restore before the attention exchange

        print("[INFO] Sending 'bio ready'...")
        send_command(ser, "bio ready", ok_responses=("OK",), label="bio ready")
        print("[OK]  Device ready")

    finally:
        ser.timeout = saved_timeout        # restore unconditionally on any exit path


def send_pin_config(ser: serial.Serial, pins: List[int], line_delay: float) -> None:
    pin_str = " ".join(str(p) for p in pins)
    line    = f"bio pin {pin_str}"
    print(f"[INFO] Sending pin config: {line}")
    send_command(ser, line, ok_responses=("OK",), label="bio pin")
    print("[OK]  Pin config accepted")
    if line_delay > 0:
        time.sleep(line_delay)


def send_clk_config(ser: serial.Serial, hz: int, line_delay: float) -> None:
    line = f"bio clk {hz}"
    print(f"[INFO] Sending clock config: {line}  ({hz / 1_000_000:.3f} MHz)")
    send_command(ser, line, ok_responses=("OK",), label="bio clk")
    print("[OK]  Clock config accepted")
    if line_delay > 0:
        time.sleep(line_delay)


def send_bio(
    port: str,
    code_path: str,
    pins: Optional[List[int]],
    hz: Optional[int],
    line_delay: float,
) -> None:
    # -- Load binary blob -----------------------------------------------------
    try:
        code_bytes = Path(code_path).read_bytes()
    except OSError as e:
        sys.exit(f"[ERROR] Cannot read code file: {e}")

    if len(code_bytes) == 0:
        sys.exit("[ERROR] Code file is empty")

    if len(code_bytes) > MAX_CODE_BYTES:
        sys.exit(
            f"[ERROR] Code file is {len(code_bytes)} bytes; "
            f"maximum allowed is {MAX_CODE_BYTES} bytes"
        )

    # Build only the chunks actually needed.  The final chunk is zero-padded
    # to CHUNK_DATA_SIZE if the data doesn't fill it exactly.  Any remaining
    # slots on the device are filled via the "bio pad" command sent afterwards.
    n_chunks = -(-len(code_bytes) // CHUNK_DATA_SIZE)  # ceiling division
    chunks = []
    for i in range(n_chunks):
        chunk = code_bytes[i * CHUNK_DATA_SIZE : (i + 1) * CHUNK_DATA_SIZE]
        if len(chunk) < CHUNK_DATA_SIZE:
            chunk = chunk.ljust(CHUNK_DATA_SIZE, b"\x00")
        chunks.append(chunk)

    needs_pad = n_chunks < NUM_CHUNKS

    # -- Open port ------------------------------------------------------------
    ser = open_port(port)

    try:
        wait_for_ready(ser)

        # -- Optional config directives (sent before the code chunks) ---------
        if pins is not None:
            send_pin_config(ser, pins, line_delay)

        if hz is not None:
            send_clk_config(ser, hz, line_delay)

        # -- Upload chunks ----------------------------------------------------
        pad_note = f", then 'pad' for remaining {NUM_CHUNKS - n_chunks}" if needs_pad else ""
        print(f"[INFO] Uploading {n_chunks}/{NUM_CHUNKS} chunks via {port} @ {BAUD_RATE} baud{pad_note}")

        for idx, chunk_data in enumerate(chunks):
            wire    = make_chunk(idx, chunk_data)
            encoded = base64.b64encode(wire).decode("ascii")
            line    = f"bio {encoded}\n"
            line_b  = line.encode("ascii")

            for attempt in range(MAX_RETRIES + 1):
                ser.write(line_b)

                response = readline_skip_echo(ser)

                if response == "SUCCESS":
                    # Only reachable when needs_pad is False (all 64 chunks sent).
                    print(f"[OK]  Chunk {idx + 1:>2}/{n_chunks} -> SUCCESS - transfer complete")
                    send_command(ser, "bio reload", ok_responses=("BIO load successful",), label="bio reload")
                    print("[OK]  Reload complete")
                    return

                if response == "OK":
                    print(f"[OK]  Chunk {idx + 1:>2}/{n_chunks}")
                    break

                if response == "ERR":
                    if attempt < MAX_RETRIES:
                        print(f"[WARN] Chunk {idx + 1:>2}/{n_chunks} ERR - retry {attempt + 1}/{MAX_RETRIES}")
                        time.sleep(RETRY_DELAY)
                    else:
                        sys.exit(
                            f"[ERROR] Chunk {idx + 1}/{n_chunks} failed "
                            f"after {MAX_RETRIES} retries"
                        )
                else:
                    if attempt < MAX_RETRIES:
                        print(
                            f"[WARN] Chunk {idx + 1:>2}/{n_chunks} unexpected response "
                            f"{response!r} - retry {attempt + 1}/{MAX_RETRIES}"
                        )
                        time.sleep(RETRY_DELAY)
                    else:
                        sys.exit(
                            f"[ERROR] Chunk {idx + 1}/{n_chunks} - "
                            f"no valid response after retries"
                        )

            if line_delay > 0:
                time.sleep(line_delay)

        if needs_pad:
            print(f"[INFO] Sending pad command ({NUM_CHUNKS - n_chunks} slots zero-filled on device)")
            send_command(ser, "bio pad", ok_responses=("SUCCESS",), label="bio pad")
            print("[OK]  Pad accepted - transfer complete")
            send_command(ser, "bio reload", ok_responses=("BIO load successful",), label="bio reload")
            print("[OK]  Reload complete")
        else:
            print("[WARN] All chunks sent but SUCCESS was never received")

    finally:
        ser.close()


def clear_bio(port: str, line_delay: float) -> None:
    ser = open_port(port)
    print("[INFO] Sending bio clear...")
    try:
        send_command(ser, "bio clear", ok_responses=("CLEAR",), label="bio clear")
        print("[OK]  BIO program cleared")
        if line_delay > 0:
            time.sleep(line_delay)
    finally:
        ser.close()


# -- Interactive console -------------------------------------------------------
CONSOLE_DRAIN_TIMEOUT = 0.20

def console_mode(port: str) -> None:
    """
    *Experimental* interactive REPL console over serial.

    Line editing and command history (up/down arrows, Ctrl-R reverse search)
    are provided by Python's readline module on macOS/Linux.  On Windows,
    install pyreadline3 for the same effect; without it, input() still works
    but history is unavailable.

    After each command the device's response is drained: we keep reading
    lines until CONSOLE_DRAIN_TIMEOUT seconds of silence, then re-prompt.

    Exit with Ctrl-D (EOF) or Ctrl-C.
    """
    # Importing readline as a side-effect is all that is needed - it monkey-
    # patches the built-in input() to add GNU Readline key-bindings and
    # persistent history for the lifetime of the process.
    try:
        import readline
        readline.set_history_length(500)
    except ImportError:
        print("[WARN] readline unavailable - no history/arrow-key support")
        print("       (Windows: pip install pyreadline3)")

    ser = open_port(port)
    # _readline manages its own deadline; no need to set ser.timeout here.
    # Leave it at the default (SERIAL_TIMEOUT) so send_command still works
    # if ever called from this context.

    print("[INFO] Console ready. Ctrl-D or Ctrl-C to exit.")
    print()

    try:
        while True:
            # ---- Prompt and read a line (readline gives us history for free) ----
            try:
                line = input("> ")
            except EOFError:          # Ctrl-D
                print()
                break
            except KeyboardInterrupt: # Ctrl-C while at the prompt
                print()
                break

            line = line.strip()
            if not line:
                continue

            # ---- Send to device ------------------------------------------------
            ser.write((line + "\n").encode("ascii"))

            # ---- Drain device response until CONSOLE_DRAIN_TIMEOUT of silence --
            try:
                while True:
                    raw = _readline(ser, CONSOLE_DRAIN_TIMEOUT)
                    if not raw:               # timed out -> device quiet
                        break
                    decoded = raw.decode("ascii", errors="replace").rstrip("\r\n")
                    if decoded.startswith("[console]"):
                        continue              # skip local echo
                    if decoded:
                        print(decoded)
            except KeyboardInterrupt:         # Ctrl-C while waiting for response
                print()                       # don't exit - just re-show prompt
                continue

    finally:
        ser.close()
        print("[INFO] Console closed.")



# -- CLI ----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Upload a BIO coprocessor program to a Dabao via serial. "
            "Optionally set I/O pins (--pad) and clock rate (--quantum) before upload."
        )
    )
    parser.add_argument(
        "--port", required=True,
        help="Serial port, e.g. /dev/ttyUSB0 or COM3",
    )
    parser.add_argument(
        "--code",
        help="Path to the BIO binary blob (<0xF00 bytes)",
    )
    parser.add_argument(
        "--pads", metavar="SLOT[,SLOT...]",
        help=(
            "Pads to map to BIO: PB1, PB2, etc.."
            "(comma- or space-separated). "
        ),
    )
    parser.add_argument(
        "--quantum", metavar="FREQ",
        help=(
            "BIO core clock rate. Accepts a number with an optional suffix: "
            "MHz (or mhz), KHz (or khz), Hz (or hz), or bare number for Hz. "
            "Examples: 350MHz  48KHz  1000  3.5MHz.  Max: 700 MHz."
        ),
    )
    parser.add_argument(
        "--delay", type=float, default=DEFAULT_LINE_DELAY, metavar="SECONDS",
        help=f"Delay between serial lines (default: {DEFAULT_LINE_DELAY}s)",
    )
    parser.add_argument(
        "--clear", action="store_true",
        help="Wipe the stored BIO program from the device. Overrides all other options.",
    )
    parser.add_argument(
        "--console", action="store_true",
        help=(
            "After uploading (or configuring), enter an interactive serial console. "
            "Can also be used standalone (without --code) to open the console directly. "
            "Up/down arrows scroll command history. Ctrl-D or Ctrl-C to exit."
        ),
    )
    args = parser.parse_args()

    # -- Clear path -----------------------------------------------------------
    if args.clear:
        print("--clear specified; ignoring all other arguments")
        clear_bio(args.port, args.delay)
        if args.console:
            console_mode(args.port)
        return

    # -- Validate config-only vs upload path ----------------------------------
    pins: Optional[List[int]] = None
    hz:   Optional[int]       = None

    if args.pads is not None:
        try:
            pins = parse_pads(args.pads)
        except argparse.ArgumentTypeError as e:
            parser.error(str(e))

    if args.quantum is not None:
        try:
            hz = parse_quantum(args.quantum)
        except argparse.ArgumentTypeError as e:
            parser.error(str(e))
    if hz is not None:
        if hz < 25_000:
            parser.error("Hz must be >= 25_000")
        if hz > 700_000_000:
            parser.error("Hz must be <= 700_000_000")

    if args.code is None:
        # Console-only mode: no upload, no config.
        if pins is None and hz is None and args.console:
            console_mode(args.port)
            return

        # Config-only mode: send pin / clk directives without uploading code.
        if pins is None and hz is None:
            parser.error(
                "Specify --code to upload, --console to open the console, "
                "or at least one of --pads / --quantum to configure"
            )
        ser = open_port(args.port)
        try:
            wait_for_ready(ser)
            if pins is not None:
                send_pin_config(ser, pins, args.delay)
            if hz is not None:
                send_clk_config(ser, hz, args.delay)
            send_command(ser, "bio reload", ok_responses=("BIO load successful",), label="bio reload")
            print("[OK]  Reload complete")
        finally:
            ser.close()
        if args.console:
            console_mode(args.port)
        return

    send_bio(args.port, args.code, pins, hz, args.delay)
    if args.console:
        console_mode(args.port)


if __name__ == "__main__":
    main()