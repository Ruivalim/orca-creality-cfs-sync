# /// script
# requires-python = ">=3.12"
#
# [tool.orcaslicer.plugin]
# name = "Creality CFS Sync"
# description = "Publishes the Creality CFS of a Creality Hi as Moonraker lane data, so Orca's filament sync sees it."
# author = "Rui Valim"
# version = "0.1.0"
# ///
"""Creality CFS Sync -- make Orca's Moonraker filament sync see the Creality CFS.

Orca's Moonraker agent syncs filaments from the `lane_data` namespace of the Moonraker database
(the format AFC and Happy Hare publish). The Creality Hi keeps its CFS state in the `box` Klipper
object instead and never writes lane data, so the sync finds nothing.

This plugin bridges the two: it reads `box` and writes one `lane_data` entry per loaded CFS slot.
Orca's sync button then works unchanged. Entries it owns carry the `cfs_` key prefix; anything else
in the namespace is left alone.

The printer is the device selected in Orca (for Moonraker the device id is `host:port`). Selecting
it syncs once and starts a background thread that refreshes the lane data every
`interval_seconds`, writing only what changed. Running the capability from the Plugins dialog
does the same at once.
"""
import http.client
import json
import threading
import urllib.parse

import orca

NAMESPACE = "lane_data"
KEY_PREFIX = "cfs_"
TIMEOUT_SECONDS = 5
# Calls made on the GUI thread freeze Orca until they return, so give up sooner there.
FOREGROUND_TIMEOUT_SECONDS = 2

_DEFAULTS = {
    "host": "",
    "interval_seconds": 15,
}

# Creality CFS material ids, as reported in box.T<n>.material_type.
MATERIALS = {
    "000001": "PLA", "000002": "ABS", "000003": "PETG", "000004": "TPU", "000005": "ASA",
    "000006": "PA", "000007": "PC", "000008": "PLA-CF", "000009": "PETG-CF", "000010": "PA-CF",
}


class MoonrakerError(Exception):
    pass


def normalize_host(value):
    """"http://host:port" from a Moonraker device id or print_host, or "" when it is not one.

    Bambu device ids are serial numbers, which are valid host names, so a bare word without a dot
    or a port is rejected rather than tried."""
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    try:
        parsed = urllib.parse.urlsplit(text)
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    if "." not in parsed.hostname and port is None:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def normalize_colour(value):
    """"#RRGGBB" from a CFS colour, or "" for an empty or invalid one.

    Creality prefixes six-digit RGB colours with a zero ("0FFFFFF")."""
    text = str(value or "").strip().lstrip("#")
    if len(text) == 7 and text[0] == "0":
        text = text[1:]
    if len(text) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in text):
        return ""
    return "#" + text.upper()


def parse_box(response):
    """Loaded CFS slots from a /printer/objects/query?box response, as {key: lane}.

    Returns None when the printer has no `box` object (not a CFS printer). Slots of disconnected
    units, empty slots and unknown materials are skipped, so Orca shows them as empty."""
    try:
        box = response["result"]["status"]["box"]
    except (KeyError, TypeError):
        return None
    if not isinstance(box, dict):
        return None

    lanes = {}
    for unit in range(1, 5):
        tray = box.get(f"T{unit}")
        if not isinstance(tray, dict) or tray.get("state") != "connect":
            continue
        materials = tray.get("material_type") or []
        colours = tray.get("color_value") or []
        for slot in range(4):
            material = MATERIALS.get(materials[slot] if slot < len(materials) else "")
            if not material:
                continue
            index = (unit - 1) * 4 + slot
            lanes[f"{KEY_PREFIX}T{unit}{'ABCD'[slot]}"] = {
                "lane": str(index),
                "material": material,
                "color": normalize_colour(colours[slot] if slot < len(colours) else ""),
            }
    return lanes


def plan_changes(current, wanted):
    """(writes, deletes) turning the namespace `current` into `wanted` for the keys we own."""
    writes = {key: lane for key, lane in wanted.items() if current.get(key) != lane}
    deletes = [key for key in current if key.startswith(KEY_PREFIX) and key not in wanted]
    return writes, sorted(deletes)


class Moonraker:
    def __init__(self, base_url, timeout=TIMEOUT_SECONDS):
        parts = urllib.parse.urlsplit(base_url)
        self._https = parts.scheme == "https"
        self._host = parts.hostname or ""
        self._port = parts.port
        self._timeout = timeout

    def request(self, method, path, body=None):
        """Decoded JSON of a Moonraker call. A 404 returns None; any other failure raises."""
        cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        conn = cls(self._host, self._port, timeout=self._timeout)
        try:
            headers = {"Content-Type": "application/json"} if body is not None else {}
            conn.request(method, path, None if body is None else json.dumps(body), headers)
            response = conn.getresponse()
            data = response.read()
        except (OSError, http.client.HTTPException) as exc:
            raise MoonrakerError(f"{method} {path}: {exc}") from exc
        finally:
            conn.close()
        if response.status == 404:
            return None
        if response.status != 200:
            raise MoonrakerError(f"{method} {path}: HTTP {response.status}")
        try:
            return json.loads(data)
        except ValueError as exc:
            raise MoonrakerError(f"{method} {path}: invalid JSON") from exc

    def query_box(self):
        return self.request("GET", "/printer/objects/query?box")

    def lane_data(self):
        """The namespace as a dict; empty when it does not exist yet."""
        response = self.request("GET", f"/server/database/item?namespace={NAMESPACE}")
        value = (response or {}).get("result", {}).get("value")
        return value if isinstance(value, dict) else {}

    def write_lane(self, key, lane):
        self.request("POST", "/server/database/item", {"namespace": NAMESPACE, "key": key, "value": lane})

    def delete_lane(self, key):
        query = urllib.parse.urlencode({"namespace": NAMESPACE, "key": key})
        self.request("DELETE", f"/server/database/item?{query}")


def sync(client):
    """Bring the printer's lane data in line with its CFS. Returns a one-line summary."""
    lanes = parse_box(client.query_box())
    if lanes is None:
        return "no CFS on this printer, nothing written"
    writes, deletes = plan_changes(client.lane_data(), lanes)
    for key, lane in writes.items():
        client.write_lane(key, lane)
    for key in deletes:
        client.delete_lane(key)
    loaded = ", ".join(f"{key[len(KEY_PREFIX):]} {lane['material']} {lane['color']}"
                       for key, lane in sorted(lanes.items()))
    return f"{len(lanes)} slot(s) [{loaded}]: {len(writes)} written, {len(deletes)} removed"


class CfsSync(orca.script.ScriptPluginCapabilityBase):
    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()  # one sync at a time: poller and Run never interleave writes
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    def get_name(self):
        return "Creality CFS Sync"

    def get_default_config(self):
        return _DEFAULTS

    def _settings(self):
        try:
            config = json.loads(self.get_config())
        except (TypeError, ValueError):
            config = {}
        return {**_DEFAULTS, **(config if isinstance(config, dict) else {})}

    def _interval(self):
        try:
            return max(5.0, float(self._settings()["interval_seconds"]))
        except (TypeError, ValueError):
            return float(_DEFAULTS["interval_seconds"])

    def _sync_now(self, timeout=TIMEOUT_SECONDS):
        base_url = normalize_host(self._settings()["host"])
        if not base_url:
            return None
        with self._lock:
            return sync(Moonraker(base_url, timeout))

    def _run(self):
        while not self._stop.is_set():
            try:
                self._sync_now()
            except Exception as exc:  # the printer may be off; try again next round
                print(f"[Creality CFS Sync] {exc}")
            self._wake.wait(self._interval())
            self._wake.clear()

    def _poller_running(self):
        return self._thread is not None and self._thread.is_alive()

    def _start_in_foreground(self):
        """First sync and poller start, from a GUI-thread call (DeviceSelected, Run).

        Orca asks for the thread and network permissions with a modal on the GUI thread and saves
        the answer. Asked from any other thread, it denies the call and shows the prompt later,
        and for a local plugin that answer is not saved, so on_load (a loader thread) must not
        open a socket or start a thread. Returns the sync summary; raises what the sync raised."""
        try:
            return self._sync_now(timeout=FOREGROUND_TIMEOUT_SECONDS)
        finally:
            if not self._poller_running():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="creality-cfs-sync", daemon=True)
                self._thread.start()

    def on_unload(self):
        self._stop.set()
        self._wake.set()

    def on_lifecycle_event(self, event, context):
        if event != orca.LifecycleEvent.DeviceSelected:
            return
        host = normalize_host(getattr(context, "name", "") or getattr(context, "device_id", ""))
        if not host:
            return
        config = self._settings()
        if config["host"] != host:
            config["host"] = host
            self.save_config(json.dumps(config))
        if self._poller_running():
            self._wake.set()
            return
        try:
            self._start_in_foreground()
        except Exception as exc:  # printer off or permission refused; Run retries
            print(f"[Creality CFS Sync] {exc}")

    def execute(self):
        try:
            summary = self._start_in_foreground()
        except Exception as exc:
            return orca.ExecutionResult.failure(f"Creality CFS Sync: {exc}")
        if summary is None:
            return orca.ExecutionResult.failure(
                "Creality CFS Sync: no printer yet. Select the printer in the Device tab first.")
        return orca.ExecutionResult.success(f"Creality CFS Sync: {summary}")


@orca.plugin
class CfsSyncPackage(orca.base):
    def register_capabilities(self):
        orca.register_capability(CfsSync)
