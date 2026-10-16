"""Tests for the Creality CFS Sync plugin, against a stand-in `orca` module and a fake Moonraker.

    python3 tests/test_creality_cfs_sync.py
"""
import importlib.util
import json
import pathlib
import socket
import sys
import threading
import time
import types
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PLUGIN = pathlib.Path(__file__).resolve().parent.parent / "creality_cfs_sync_any.py"


def _install_fake_orca():
    orca = types.ModuleType("orca")

    class _Base:
        def __init__(self):
            self._config = "{}"

        def get_config(self):
            return self._config

        def save_config(self, value):
            self._config = value
            return True

        def on_load(self):
            pass

    class _Result:
        @staticmethod
        def success(message=""):
            return ("success", message)

        @staticmethod
        def failure(message=""):
            return ("failure", message)

    orca.script = types.SimpleNamespace(ScriptPluginCapabilityBase=_Base)
    orca.base = object
    orca.plugin = lambda cls: cls
    orca.register_capability = lambda cls: None
    orca.ExecutionResult = _Result
    orca.LifecycleEvent = types.SimpleNamespace(DeviceSelected="DeviceSelected", PresetSelected="PresetSelected")
    sys.modules["orca"] = orca


_install_fake_orca()
_spec = importlib.util.spec_from_file_location("cfs", PLUGIN)
cfs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cfs)


def tray(materials, colours, state="connect"):
    return {"state": state, "material_type": materials, "color_value": colours}


def box_response(**trays):
    return {"result": {"status": {"box": {"state": "connect", **trays}}}}


# What the Creality Hi answered on 2026-10-15, trimmed to the fields the plugin reads.
HI_BOX = box_response(
    T1=tray(["000001", "000001", "000003", "000003"], ["03DDF57", "0FFFFFF", "0000000", "0FFFFFF"]),
    T2=tray(["-1"] * 4, ["-1"] * 4, state="None"),
    T3=tray(["-1"] * 4, ["-1"] * 4, state="None"),
    T4=tray(["-1"] * 4, ["-1"] * 4, state="None"),
)


class FakeMoonraker:
    """A Moonraker that keeps its database in memory and records every call."""

    def __init__(self, box=HI_BOX):
        self.box = box
        self.db = {}  # namespace -> {key: value}
        self.calls = []
        self.fail_with = None  # HTTP status to answer every call with
        self.delay = 0.0
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self, status, payload):
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client gave up first (timeout test)

            def _handle(self, method):
                url = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(url.query))
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                with fake.lock:
                    fake.calls.append((method, url.path, query, body))
                if fake.delay:
                    time.sleep(fake.delay)
                if fake.fail_with:
                    return self._answer(fake.fail_with, b"boom" if fake.fail_with == 200 else {"error": "x"})
                with fake.lock:
                    return self._answer(*fake.route(method, url.path, query, body))

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_DELETE(self):
                self._handle("DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def route(self, method, path, query, body):
        if method == "GET" and path == "/printer/objects/query":
            return (200, self.box) if self.box is not None else (200, {"result": {"status": {}}})
        if path == "/server/database/item":
            if method == "GET":
                space = self.db.get(query["namespace"])
                if space is None:
                    return 404, {"error": {"code": 404, "message": f"Namespace '{query['namespace']}' not found"}}
                return 200, {"result": {"namespace": query["namespace"], "value": space}}
            if method == "POST":
                self.db.setdefault(body["namespace"], {})[body["key"]] = body["value"]
                return 200, {"result": {"namespace": body["namespace"], "key": body["key"], "value": body["value"]}}
            if method == "DELETE":
                value = self.db.get(query["namespace"], {}).pop(query["key"], None)
                return (200, {"result": {"value": value}}) if value is not None else (404, {"error": "x"})
        return 404, {"error": "unknown"}

    def writes(self):
        return [c for c in self.calls if c[0] in ("POST", "DELETE")]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class NormalizeHost(unittest.TestCase):
    def test_moonraker_device_id_becomes_a_base_url(self):
        self.assertEqual(cfs.normalize_host("192.168.15.85:4408"), "http://192.168.15.85:4408")
        self.assertEqual(cfs.normalize_host(" https://hi.lan:7125/ "), "https://hi.lan:7125")
        self.assertEqual(cfs.normalize_host("hi.local"), "http://hi.local")

    def test_bambu_serial_and_garbage_are_not_hosts(self):
        for value in ("03900D630625384", "", None, "   ", "ftp://x.y", "http://", "a b:c"):
            self.assertEqual(cfs.normalize_host(value), "", value)


class NormalizeColour(unittest.TestCase):
    def test_strips_creality_leading_zero_and_uppercases(self):
        self.assertEqual(cfs.normalize_colour("03ddf57"), "#3DDF57")
        self.assertEqual(cfs.normalize_colour("#ff0000"), "#FF0000")

    def test_empty_or_invalid_colour_is_empty(self):
        for value in ("-1", "", None, "None", "12345", "0GGGGGG", "1FFFFFF"):
            self.assertEqual(cfs.normalize_colour(value), "", value)


class ParseBox(unittest.TestCase):
    def test_the_hi_reports_four_slots_on_t1(self):
        lanes = cfs.parse_box(HI_BOX)
        self.assertEqual(lanes, {
            "cfs_T1A": {"lane": "0", "material": "PLA", "color": "#3DDF57"},
            "cfs_T1B": {"lane": "1", "material": "PLA", "color": "#FFFFFF"},
            "cfs_T1C": {"lane": "2", "material": "PETG", "color": "#000000"},
            "cfs_T1D": {"lane": "3", "material": "PETG", "color": "#FFFFFF"},
        })

    def test_second_unit_numbers_its_lanes_after_the_first(self):
        lanes = cfs.parse_box(box_response(T2=tray(["-1", "000004"], ["-1", "0FF0000"])))
        self.assertEqual(lanes, {"cfs_T2B": {"lane": "5", "material": "TPU", "color": "#FF0000"}})

    def test_empty_and_unknown_slots_keep_their_lane_numbers_free(self):
        lanes = cfs.parse_box(box_response(T1=tray(["000001", "-1", "999999", "000002"], ["0111111"] * 4)))
        self.assertEqual(sorted(lane["lane"] for lane in lanes.values()), ["0", "3"])

    def test_disconnected_unit_is_ignored(self):
        self.assertEqual(cfs.parse_box(box_response(T1=tray(["000001"] * 4, ["0FFFFFF"] * 4, state="disconnect"))), {})

    def test_short_or_missing_arrays_do_not_raise(self):
        lanes = cfs.parse_box(box_response(T1={"state": "connect", "material_type": ["000001"]}))
        self.assertEqual(lanes, {"cfs_T1A": {"lane": "0", "material": "PLA", "color": ""}})
        self.assertEqual(cfs.parse_box(box_response(T1={"state": "connect", "material_type": None})), {})

    def test_printer_without_box_is_not_a_cfs_printer(self):
        for response in (None, {}, {"result": {"status": {}}}, {"result": {"status": {"box": None}}}, []):
            self.assertIsNone(cfs.parse_box(response), response)


class PlanChanges(unittest.TestCase):
    def test_only_changed_lanes_are_written(self):
        current = {"cfs_T1A": {"lane": "0", "material": "PLA", "color": "#FFFFFF"}}
        wanted = {"cfs_T1A": {"lane": "0", "material": "PLA", "color": "#FFFFFF"},
                  "cfs_T1B": {"lane": "1", "material": "ABS", "color": "#000000"}}
        self.assertEqual(cfs.plan_changes(current, wanted), ({"cfs_T1B": wanted["cfs_T1B"]}, []))

    def test_lanes_of_other_owners_are_never_deleted(self):
        current = {"lane1": {"lane": "0"}, "cfs_T1C": {"lane": "2"}}
        self.assertEqual(cfs.plan_changes(current, {}), ({}, ["cfs_T1C"]))


class SyncAgainstMoonraker(unittest.TestCase):
    def setUp(self):
        self.printer = FakeMoonraker()
        self.client = cfs.Moonraker(self.printer.url)

    def tearDown(self):
        self.printer.close()

    def test_first_sync_creates_the_namespace_then_a_second_sync_writes_nothing(self):
        summary = cfs.sync(self.client)
        self.assertIn("4 written", summary)
        self.assertEqual(self.printer.db["lane_data"], cfs.parse_box(HI_BOX))
        before = len(self.printer.writes())
        self.assertIn("0 written, 0 removed", cfs.sync(self.client))
        self.assertEqual(len(self.printer.writes()), before)

    def test_the_lane_data_matches_what_orca_reads(self):
        # MoonrakerPrinterAgent::fetch_moonraker_filament_data reads "lane" (string), "material"
        # and "color" from every entry of the namespace.
        cfs.sync(self.client)
        for lane in self.printer.db["lane_data"].values():
            self.assertIsInstance(lane["lane"], str)
            int(lane["lane"])
            self.assertTrue(lane["material"])
            self.assertRegex(lane["color"], r"^#[0-9A-F]{6}$")

    def test_an_emptied_slot_is_removed_and_foreign_lanes_survive(self):
        self.printer.db["lane_data"] = {"afc_lane": {"lane": "9", "material": "PLA", "color": "#123456"}}
        cfs.sync(self.client)
        self.printer.box = box_response(T1=tray(["000001", "-1", "000003", "000003"],
                                                ["03DDF57", "-1", "0000000", "0FFFFFF"]))
        self.assertIn("1 removed", cfs.sync(self.client))
        self.assertNotIn("cfs_T1B", self.printer.db["lane_data"])
        self.assertIn("afc_lane", self.printer.db["lane_data"])

    def test_a_printer_without_cfs_gets_nothing_written(self):
        self.printer.box = None
        self.assertIn("no CFS", cfs.sync(self.client))
        self.assertEqual(self.printer.writes(), [])

    def test_http_errors_raise_without_writing(self):
        for status in (500, 401, 200):  # 200 here answers a body that is not JSON
            self.printer.fail_with = status
            with self.assertRaises(cfs.MoonrakerError):
                cfs.sync(self.client)
        self.assertEqual(self.printer.writes(), [])

    def test_unreachable_printer_raises(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with self.assertRaises(cfs.MoonrakerError):
            cfs.sync(cfs.Moonraker(f"http://127.0.0.1:{port}"))

    def test_a_hung_printer_times_out(self):
        self.printer.delay = 2.0
        started = time.monotonic()
        with self.assertRaises(cfs.MoonrakerError):
            cfs.sync(cfs.Moonraker(self.printer.url, timeout=0.3))
        self.assertLess(time.monotonic() - started, 1.5)


class Capability(unittest.TestCase):
    def setUp(self):
        self.printer = FakeMoonraker()
        self.cap = cfs.CfsSync()

    def tearDown(self):
        self.cap.on_unload()
        self.printer.close()

    def event(self, name):
        return types.SimpleNamespace(name=name, device_id="")

    def test_run_without_a_selected_printer_fails_politely(self):
        status, message = self.cap.execute()
        self.assertEqual(status, "failure")
        self.assertIn("Device tab", message)

    def test_selecting_a_bambu_printer_changes_nothing(self):
        self.cap.on_lifecycle_event("DeviceSelected", self.event("03900D630625384"))
        self.assertEqual(json.loads(self.cap.get_config()), {})

    def test_other_events_are_ignored(self):
        self.cap.on_lifecycle_event("PresetSelected", self.event("192.168.15.85:4408"))
        self.assertEqual(json.loads(self.cap.get_config()), {})

    def test_selecting_the_printer_remembers_it_and_run_syncs(self):
        self.cap.on_lifecycle_event("DeviceSelected", self.event(self.printer.url))
        self.assertEqual(json.loads(self.cap.get_config())["host"], self.printer.url)
        status, message = self.cap.execute()
        self.assertEqual(status, "success", message)
        self.assertIn("T1A PLA #3DDF57", message)

    def test_run_reports_an_unreachable_printer_as_failure(self):
        self.cap.save_config(json.dumps({"host": "127.0.0.1:1"}))
        status, _ = self.cap.execute()
        self.assertEqual(status, "failure")

    def test_corrupt_config_falls_back_to_defaults(self):
        self.cap.save_config("not json")
        self.assertEqual(self.cap._settings(), cfs._DEFAULTS)
        self.cap.save_config(json.dumps({"interval_seconds": "soon"}))
        self.assertEqual(self.cap._interval(), 15.0)
        self.cap.save_config(json.dumps({"interval_seconds": 0}))
        self.assertEqual(self.cap._interval(), 5.0)

    def test_concurrent_syncs_do_not_write_twice(self):
        self.cap.save_config(json.dumps({"host": self.printer.url}))
        self.printer.delay = 0.05  # widen the window between reading and writing the namespace
        threads = [threading.Thread(target=self.cap.execute) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len([c for c in self.printer.writes() if c[0] == "POST"]), 4)

    def test_loading_starts_no_thread_and_opens_no_socket(self):
        # Orca loads plugins on a worker thread, where an audited thread start or connection is
        # denied and the user's later "allow" is not saved for a local plugin. Watch the same
        # audit events Orca's hook does.
        self.cap.save_config(json.dumps({"host": self.printer.url}))
        seen = []
        watching = [True]
        sys.addaudithook(lambda event, args: watching[0] and event in (
            "_thread.start_new_thread", "socket.connect", "http.client.connect") and seen.append(event))
        try:
            self.cap.on_load()
        finally:
            watching[0] = False  # audit hooks cannot be removed
        self.assertEqual(seen, [])
        self.assertIsNone(self.cap._thread)

    def test_selecting_the_printer_syncs_at_once_and_starts_the_poller(self):
        self.cap.on_lifecycle_event("DeviceSelected", self.event(self.printer.url))
        # Synchronous: the data is there when the handler returns, no waiting on the poller.
        self.assertEqual(len(self.printer.db.get("lane_data", {})), 4)
        self.assertTrue(self.cap._poller_running())

    def test_reselecting_while_polling_only_wakes_the_poller(self):
        self.cap.on_lifecycle_event("DeviceSelected", self.event(self.printer.url))
        self.printer.delay = 0.5  # a foreground sync would now block the handler this long
        started = time.monotonic()
        self.cap.on_lifecycle_event("DeviceSelected", self.event(self.printer.url))
        self.assertLess(time.monotonic() - started, 0.2)

    def test_selecting_an_unreachable_printer_does_not_raise_and_keeps_polling(self):
        self.cap.on_lifecycle_event("DeviceSelected", self.event("127.0.0.1:1"))
        self.assertTrue(self.cap._poller_running())

    def test_a_refused_thread_permission_does_not_break_the_handler_or_run(self):
        class RefusedThread(threading.Thread):
            def start(self):
                raise PermissionError('audited operation "_thread.start_new_thread" without permission')

        # Only the plugin's threads are refused; the fake Moonraker keeps its own.
        original = cfs.threading
        cfs.threading = types.SimpleNamespace(Thread=RefusedThread)
        try:
            self.cap.on_lifecycle_event("DeviceSelected", self.event(self.printer.url))
            status, message = self.cap.execute()
        finally:
            cfs.threading = original
        self.assertEqual(status, "failure")
        self.assertIn("permission", message)
        self.assertFalse(self.cap._poller_running())
        # The sync itself went through before the thread was refused.
        self.assertEqual(len(self.printer.db["lane_data"]), 4)

    def test_the_poller_picks_up_a_changed_cfs_and_stops_on_unload(self):
        self.cap.save_config(json.dumps({"host": self.printer.url, "interval_seconds": 5}))
        self.cap.on_lifecycle_event("DeviceSelected", self.event(self.printer.url))
        self.printer.box = box_response(T1=tray(["000002", "-1", "-1", "-1"], ["0FF0000", "-1", "-1", "-1"]))
        self.cap._wake.set()  # stands in for the interval elapsing
        deadline = time.monotonic() + 3
        while len(self.printer.db["lane_data"]) != 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.printer.db["lane_data"],
                         {"cfs_T1A": {"lane": "0", "material": "ABS", "color": "#FF0000"}})
        self.cap.on_unload()
        self.cap._thread.join(timeout=2)
        self.assertFalse(self.cap._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
