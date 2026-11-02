"""GPS95 hardware bridge.

The AIO v2's Ai-Thinker GP-02 is a CASIC receiver that normally emits NMEA,
but gpsd's u-blox autoconfiguration can switch it into a binary-only mode
where gpsd reports TPV but no SKY satellites (the xgps "no data" bug), and
the receiver stays in that mode after gpsd exits (the PyGPSClient bug).

This bridge is the single owner of /dev/serial0.  It:
  * restores the GP-02 NMEA profile with PCAS commands,
  * keeps GPS+GLONASS+BeiDou enabled (PCAS04,7),
  * filters the binary frames out,
  * exposes clean NMEA on a PTY for gpsd and on TCP for direct-mode clients.

gpsd therefore only ever sees NMEA and can always build a complete SKY
message.  If gpsd is not installed, clients can read TCP 2958 directly.
"""

from __future__ import annotations

import errno
import json
import os
import pty
import queue
import selectors
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import traceback

import serial

from .model import GPSState
from .nmea import NMEAParser, checksum, checksum_ok

RUN_DIR = os.environ.get("GPS95_RUN", "/run/gps95")
LOG_DIR = os.environ.get("GPS95_LOG", "/var/log/gps95")
SERIAL_PORT = os.environ.get("GPS95_SERIAL", "/dev/serial0")
BAUD = int(os.environ.get("GPS95_BAUD", "9600"))
GNSS_MODE = int(os.environ.get("GPS95_GNSS_MODE", "7"))
NMEA_PORT = int(os.environ.get("GPS95_NMEA_PORT", "2958"))
CONTROL_SOCK = os.path.join(RUN_DIR, "control.sock")
PTY_LINK = os.path.join(RUN_DIR, "nmea")
STATUS_FILE = os.path.join(RUN_DIR, "status.json")
CONFIG_FILE = os.environ.get("GPS95_BRIDGE_CONFIG", "/etc/gps95/bridge.json")


def pcas(body: str) -> bytes:
    ck = 0
    for ch in body:
        ck ^= ord(ch)
    return f"${body}*{ck:02X}\r\n".encode("ascii")


PCAS_NMEA = pcas("PCAS03,1,1,1,1,1,1,1,1,0,0,0,0")
PCAS_MODE = lambda mode: pcas(f"PCAS04,{int(mode)}")
PCAS_RESTART = pcas("PCAS10,2")


class Client:
    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.queue: queue.Queue = queue.Queue(maxsize=120)

    def send(self, line: bytes):
        try:
            self.queue.put_nowait(line)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(line)
            except queue.Full:
                pass


class NmeaTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, bridge):
        self.bridge = bridge
        super().__init__(addr, NmeaHandler)


class NmeaHandler(socketserver.BaseRequestHandler):
    def handle(self):
        bridge = self.server.bridge
        client = Client(self.request, self.client_address)
        bridge.add_client(client)
        try:
            self.request.sendall(b"# GPS95 NMEA 0183 stream\r\n")
            while not bridge.stop_event.is_set():
                try:
                    line = client.queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                self.request.sendall(line)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            bridge.remove_client(client)


class HardwareBridge:
    def __init__(self):
        self.state = GPSState()
        self.parser = NMEAParser(self.state)
        self.stop_event = threading.Event()
        self.serial = None
        self.pty_master = None
        self.pty_slave = None
        self.clients: set[Client] = set()
        self.clients_lock = threading.Lock()
        self.status_lock = threading.Lock()
        self.nmea_count = 0
        self.bad_count = 0
        self.last_nmea = 0.0
        self.last_gga = 0.0
        self.last_gsv = 0.0
        self.last_line = ""
        self.serial_error = ""
        self.serial_ok = False
        self.mode = GNSS_MODE
        self.recoveries = 0
        self.last_recovery = 0.0
        self.last_powercycle = 0.0
        self.started = time.time()
        self.control_ready = False
        self.tcp_server = None
        self.gpsd_restart_lock = threading.Lock()
        self._last_status_write = 0.0

    # ------------------------------------------------------------------
    # lifecycle
    def start(self):
        os.makedirs(RUN_DIR, exist_ok=True)
        os.makedirs(LOG_DIR, exist_ok=True)
        try:
            os.chmod(RUN_DIR, 0o755)
        except OSError:
            pass
        self.mode = self._load_mode()
        self._create_pty()
        self._start_tcp()
        self._start_control()
        self._open_serial()
        threading.Thread(target=self._serial_loop, name="serial-reader", daemon=True).start()
        threading.Thread(target=self._supervisor, name="bridge-supervisor", daemon=True).start()
        threading.Thread(target=self._status_loop, name="bridge-status", daemon=True).start()
        self._log("bridge started")

    def stop(self):
        self.stop_event.set()
        try:
            if self.tcp_server:
                self.tcp_server.shutdown()
                self.tcp_server.server_close()
        except Exception:
            pass
        try:
            self._close_serial()
        except Exception:
            pass
        try:
            if self.pty_master is not None:
                os.close(self.pty_master)
        except OSError:
            pass
        try:
            if os.path.exists(CONTROL_SOCK):
                os.unlink(CONTROL_SOCK)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # configuration / logging
    def _load_mode(self):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            mode = int(data.get("gnss_mode", GNSS_MODE))
            if mode in (1, 2, 3, 4, 5, 6, 7):
                return mode
        except Exception:
            pass
        return GNSS_MODE

    def _save_mode(self):
        try:
            os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
            tmp = CONFIG_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"gnss_mode": self.mode}, fh)
                fh.write("\n")
            os.replace(tmp, CONFIG_FILE)
        except Exception:
            pass

    def _log(self, text, level="info"):
        self.state.log(text, level)
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {text}\n"
        try:
            path = os.path.join(LOG_DIR, "bridge.log")
            if os.path.exists(path) and os.path.getsize(path) > 5 * 1024 * 1024:
                os.replace(path, path + ".1")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:
            pass
        print(line.rstrip(), flush=True)

    # ------------------------------------------------------------------
    # PTY
    def _create_pty(self):
        master, slave = pty.openpty()
        os.set_blocking(master, False)
        try:
            os.chmod(os.ttyname(slave), 0o666)
        except OSError:
            pass
        try:
            if os.path.islink(PTY_LINK) or os.path.exists(PTY_LINK):
                os.unlink(PTY_LINK)
        except OSError:
            pass
        os.symlink(os.ttyname(slave), PTY_LINK)
        self.pty_master = master
        self.pty_slave = slave
        self._log(f"NMEA PTY ready: {PTY_LINK} -> {os.ttyname(slave)}")

    def _write_pty(self, line: bytes):
        if self.pty_master is None:
            return
        try:
            os.write(self.pty_master, line)
        except BlockingIOError:
            pass
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EBADF):
                self._log(f"PTY write failed ({exc}); recreating", "warn")
                try:
                    self._create_pty()
                    self._restart_gpsd_soon()
                except Exception as exc2:
                    self._log(f"PTY recreate failed: {exc2}", "error")

    def _restart_gpsd_soon(self):
        def run():
            if not self.gpsd_restart_lock.acquire(blocking=False):
                return
            try:
                time.sleep(1.0)
                subprocess.run(["systemctl", "try-restart", "gpsd.service"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=15)
            except Exception:
                pass
            finally:
                self.gpsd_restart_lock.release()
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------
    # serial
    def _open_serial(self):
        try:
            self._close_serial()
        except Exception:
            pass
        try:
            ser = serial.Serial(SERIAL_PORT, BAUD, timeout=0.2, write_timeout=1.0)
            ser.reset_input_buffer()
            self.serial = ser
            self.serial_ok = True
            self.serial_error = ""
            self._log(f"serial open {SERIAL_PORT} @ {BAUD}")
            self._apply_profile(reset=False)
        except Exception as exc:
            self.serial = None
            self.serial_ok = False
            self.serial_error = str(exc)[:300]
            self._log(f"serial open failed: {exc}", "error")

    def _close_serial(self):
        if self.serial is not None:
            try:
                self.serial.close()
            except Exception:
                pass
            self.serial = None

    def _apply_profile(self, reset=False):
        if self.serial is None:
            return
        try:
            if reset:
                # PCAS10,2 reloads the factory NMEA profile, including GGA and
                # GST.  Do NOT follow it with PCAS03: on GP-02 firmware,
                # enabling several sentences with PCAS03 suppresses GGA.
                self.serial.write(PCAS_RESTART)
                self.serial.flush()
                time.sleep(4.0)
            self.serial.write(PCAS_MODE(self.mode))
            self.serial.flush()
            self._log(f"PCAS profile applied (mode={self.mode}{', reset' if reset else ''})")
        except Exception as exc:
            self._log(f"PCAS profile failed: {exc}", "error")

    def _serial_loop(self):
        while not self.stop_event.is_set():
            if self.serial is None:
                self._open_serial()
                if self.serial is None:
                    time.sleep(3.0)
                    continue
            try:
                chunk = self.serial.read(8192)
                if chunk:
                    self._consume(chunk)
            except Exception as exc:
                self.serial_ok = False
                self.serial_error = str(exc)[:300]
                self._log(f"serial read error: {exc}", "error")
                self._close_serial()
                time.sleep(2.0)

    def _consume(self, chunk: bytes):
        self._buffer = getattr(self, "_buffer", b"") + chunk
        while b"\n" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\n", 1)
            raw = raw.strip(b"\r\x00 ")
            if not raw or not raw.startswith(b"$"):
                continue
            try:
                line = raw.decode("ascii", "ignore")
            except Exception:
                continue
            if not checksum_ok(line):
                self.bad_count += 1
                continue
            self.nmea_count += 1
            self.last_nmea = time.time()
            self.last_line = line
            kind = line[3:6]
            if kind == "GGA":
                self.last_gga = self.last_nmea
            elif kind == "GSV":
                self.last_gsv = self.last_nmea
            wire = line.encode("ascii", "ignore") + b"\r\n"
            self._write_pty(wire)
            self._broadcast(wire)
            self.parser.feed(line)
            if kind in ("GGA", "RMC"):
                self.state.last_update = self.last_nmea

    # ------------------------------------------------------------------
    # clients
    def add_client(self, client: Client):
        with self.clients_lock:
            self.clients.add(client)
        self._log(f"NMEA client connected: {client.addr}")

    def remove_client(self, client: Client):
        with self.clients_lock:
            self.clients.discard(client)
        self._log(f"NMEA client disconnected: {client.addr}")

    def _broadcast(self, line: bytes):
        with self.clients_lock:
            clients = list(self.clients)
        for client in clients:
            client.send(line)

    def _start_tcp(self):
        try:
            self.tcp_server = NmeaTCPServer(("127.0.0.1", NMEA_PORT), self)
            threading.Thread(target=self.tcp_server.serve_forever,
                             name="nmea-tcp", daemon=True).start()
            self._log(f"NMEA TCP listening on 127.0.0.1:{NMEA_PORT}")
        except Exception as exc:
            self._log(f"NMEA TCP failed: {exc}", "error")

    # ------------------------------------------------------------------
    # control socket
    def _start_control(self):
        def serve():
            try:
                if os.path.exists(CONTROL_SOCK):
                    os.unlink(CONTROL_SOCK)
                srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                srv.bind(CONTROL_SOCK)
                os.chmod(CONTROL_SOCK, 0o666)
                srv.listen(8)
                srv.settimeout(1.0)
                self.control_ready = True
                while not self.stop_event.is_set():
                    try:
                        conn, _ = srv.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    threading.Thread(target=self._control_conn, args=(conn,), daemon=True).start()
                srv.close()
            except Exception as exc:
                self._log(f"control socket failed: {exc}", "error")

        threading.Thread(target=serve, name="control", daemon=True).start()

    def _control_conn(self, conn):
        try:
            conn.settimeout(3.0)
            data = conn.recv(4096)
            req = json.loads(data.decode("utf-8", "replace") or "{}")
            cmd = (req.get("cmd") or "status").lower()
            result = {"ok": True, "cmd": cmd, "status": self.status()}
            if cmd == "repair":
                self._apply_profile(reset=True)
                self._apply_profile(reset=False)
                self.recoveries += 1
                self.last_recovery = time.time()
                result["message"] = "PCAS profile restored"
            elif cmd == "set_mode":
                mode = int(req.get("mode", 7))
                if mode not in (1, 2, 3, 4, 5, 6, 7):
                    raise ValueError("mode must be 1..7")
                self.mode = mode
                self._save_mode()
                self._apply_profile(reset=False)
                result["message"] = f"GNSS mode set to {mode}"
            elif cmd == "powercycle":
                self._powercycle()
                result["message"] = "GPS rail power-cycled"
            elif cmd == "restart_gpsd":
                subprocess.run(["systemctl", "try-restart", "gpsd.service"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=15)
                result["message"] = "gpsd restarted"
            elif cmd == "restart":
                result["message"] = "bridge will restart"
                threading.Thread(target=self._delayed_exit, daemon=True).start()
            elif cmd != "status":
                result = {"ok": False, "error": f"unknown command {cmd}"}
            conn.sendall((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception as exc:
            try:
                conn.sendall((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode("utf-8"))
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _delayed_exit(self):
        time.sleep(0.3)
        os.kill(os.getpid(), signal.SIGTERM)

    # ------------------------------------------------------------------
    # recovery
    def _powercycle(self):
        now = time.time()
        if now - self.last_powercycle < 120:
            return
        self.last_powercycle = now
        self._log("power-cycling AIO v2 GPS rail (GPIO27)", "warn")
        try:
            for args in (["pinctrl", "27", "op"], ["pinctrl", "27", "dl"]):
                subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            time.sleep(2.0)
            subprocess.run(["pinctrl", "27", "dh"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5)
        except Exception as exc:
            self._log(f"powercycle failed: {exc}", "error")
        time.sleep(3.0)
        self._close_serial()
        self._open_serial()

    def _supervisor(self):
        while not self.stop_event.is_set():
            time.sleep(2.0)
            if self.serial is None:
                continue
            now = time.time()
            if now - self.started < 5:
                continue
            age = (now - self.last_nmea) if self.last_nmea else 999.0
            gga_age = (now - self.last_gga) if self.last_gga else 999.0
            gsv_age = (now - self.last_gsv) if self.last_gsv else 999.0
            nmea_ok = age < 8.0
            # GGA carries the altitude; GSV carries the satellites.  A profile
            # change that suppresses either one is treated as a bad state.
            needs_reset = False
            reason = ""
            if not nmea_ok:
                if age < 6.0:
                    continue
                needs_reset = True
                reason = f"no NMEA for {age:.0f}s"
            elif gsv_age > 18.0:
                needs_reset = True
                reason = f"no GSV for {gsv_age:.0f}s"
            if not needs_reset:
                continue
            if now - self.last_recovery < 30.0:
                continue
            self.last_recovery = now
            self.recoveries += 1
            self._log(f"{reason}; resetting GP-02 NMEA profile", "warn")
            self.last_nmea = 0.0
            self.last_gga = 0.0
            self.last_gsv = 0.0
            self._apply_profile(reset=True)
            if age > 90:
                self._powercycle()

    # ------------------------------------------------------------------
    # status
    def status(self):
        active = self.state._active_sats()
        counts, used = self.state.counts()
        return {
            "ts": time.time(),
            "serial_ok": self.serial_ok,
            "serial_port": SERIAL_PORT,
            "serial_error": self.serial_error,
            "nmea_count": self.nmea_count,
            "bad_count": self.bad_count,
            "last_nmea_age": round(time.time() - self.last_nmea, 2) if self.last_nmea else None,
            "last_gga_age": round(time.time() - self.last_gga, 2) if self.last_gga else None,
            "last_gsv_age": round(time.time() - self.last_gsv, 2) if self.last_gsv else None,
            "last_line": self.last_line,
            "fix_mode": self.state.fix_mode,
            "lat": self.state.lat,
            "lon": self.state.lon,
            "sats": len(active),
            "used": sum(1 for sat in active if sat["used"]),
            "counts": counts,
            "mode": self.mode,
            "recoveries": self.recoveries,
            "uptime": round(time.time() - self.started, 1),
            "clients": len(self.clients),
            "pty": PTY_LINK,
            "tcp_port": NMEA_PORT,
            "antenna": self.state.antenna,
            "control_ready": self.control_ready,
        }

    def _status_loop(self):
        while not self.stop_event.is_set():
            time.sleep(1.0)
            try:
                data = json.dumps(self.status(), ensure_ascii=False)
                tmp = STATUS_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(data + "\n")
                os.replace(tmp, STATUS_FILE)
            except Exception:
                pass


def main():
    bridge = HardwareBridge()

    def shutdown(_signum, _frame):
        bridge.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    bridge.start()
    while not bridge.stop_event.is_set():
        time.sleep(1)


if __name__ == "__main__":
    main()
