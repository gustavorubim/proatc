"""The view for other devices: the page, pairing, and what a paired device may and may not do.

A security review found that a paired phone could key the computer's microphone and capture keys, that the pairing
code never changed, that one source could lock every device out, and that a page request written with backslashes
made Windows open a network share (sending the user's login) before the request was refused.
"""

from __future__ import annotations

import json
import socket
import threading

import httpx
import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from proatc import config as config_module
from proatc import server
from proatc.config import Config, push_to_talk_key
from proatc.server import AppServer


class Key:
    """The push-to-talk key as the session holds it."""

    def __init__(self):
        self.pressed, self.released = threading.Event(), threading.Event()
        self.released.set()

    def press(self):
        self.released.clear()
        self.pressed.set()

    def release(self):
        self.pressed.clear()
        self.released.set()


@pytest.fixture
def app(tmp_path, monkeypatch):
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><title>Pro ATC</title>", encoding="utf-8")
    (web / "assets" / "app.js").write_text("console.log('hi')", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("not for the network", encoding="utf-8")
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(server, "PROJECT_DIR", tmp_path)
    monkeypatch.setenv("PROATC_WEB_DIR", str(web))
    config = Config()
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    config.remote.port = probe.getsockname()[1]
    probe.close()
    server_app = AppServer(config, 0, "app-token")
    server_app.session.ptt = Key()  # the keyboard listener, which needs a desktop, stood in for
    server_app.session.apply_settings({"remote": {"enabled": True}})
    server_app.set_remote(True)
    yield server_app
    server_app.set_remote(False)


def address(app) -> str:
    return f"ws://127.0.0.1:{app.session.config.remote.port}/"


def pair(app) -> str:
    """Pair a device with a code shown on the computer; return the device's own token."""
    app._command({"cmd": "pair_device"})
    code = app.remote_info()["code"]
    with connect(f"{address(app)}?token={code.lower()}") as phone:
        message = json.loads(phone.recv(timeout=5))
    assert message["type"] == "paired"
    return message["device"]


def answers(phone, wanted: str, tries: int = 20) -> dict:
    for _ in range(tries):
        message = json.loads(phone.recv(timeout=5))
        if message.get("type") == wanted:
            return message
    raise AssertionError(f"no {wanted}")


# ---- the page -------------------------------------------------------------------------------------------------


def test_the_page_is_served_and_nothing_else(app, monkeypatch):
    base = f"http://127.0.0.1:{app.session.config.remote.port}"
    page = httpx.get(base + "/")
    assert page.status_code == 200 and "Pro ATC" in page.text and page.headers["content-type"].startswith("text/html")
    assert httpx.get(base + "/assets/app.js").headers["content-type"].startswith("text/javascript")
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/nothing.css", "/C:/Windows/win.ini"):
        assert httpx.get(base + path).status_code == 404, path


def test_a_page_request_written_with_backslashes_never_reaches_the_disk(app, monkeypatch):
    # "\\host\share" joined to the page's folder is a network path on Windows, opened (with the user's login)
    # before any check. The request is now looked up by name in the list of the page's files.
    touched = []
    original = server.Path.is_file

    def watch(self):
        touched.append(str(self))
        return original(self)

    monkeypatch.setattr(server.Path, "is_file", watch)
    server.page_files()  # the list is made once; what follows must not touch the disk
    touched.clear()
    with socket.create_connection(("127.0.0.1", app.session.config.remote.port), timeout=5) as raw:
        raw.sendall(b"GET \\\\192.0.2.66\\share\\x HTTP/1.1\r\nHost: a\r\n\r\n")
        reply = raw.recv(200)
    assert b"404" in reply.split(b"\r\n")[0]
    assert not touched


# ---- pairing --------------------------------------------------------------------------------------------------


def test_there_is_no_code_until_pairing_is_opened_on_the_computer_and_each_code_pairs_one_device(app):
    assert app.remote_info()["code"] == "" and not app.pairing_open()
    with pytest.raises(ConnectionClosed):
        with connect(f"{address(app)}?token=ABCDEFGH") as stranger:
            stranger.recv(timeout=5)
    app._command({"cmd": "pair_device"})
    first = app.remote_info()["code"]
    assert len(first) == 8 and app.remote_info()["pairing_s"] > 0
    with connect(f"{address(app)}?token={first}") as phone:
        device = json.loads(phone.recv(timeout=5))
    assert device["type"] == "paired" and len(device["device"]) >= 32
    assert app.remote_info()["code"] == "" and app.remote_info()["devices"] == 1
    with pytest.raises(ConnectionClosed):  # the same code again: used up
        with connect(f"{address(app)}?token={first}") as again:
            again.recv(timeout=5)
    app._command({"cmd": "pair_device"})
    assert app.remote_info()["code"] != first  # a new code each time
    # The device's own token lets it back in, with no code.
    with connect(f"{address(app)}?token={device['device']}") as phone:
        phone.send(json.dumps({"cmd": "new_flight"}))
    # Only the hash of a device's token is kept on disk.
    assert device["device"] not in json.dumps(server.load_settings())


def test_forgetting_paired_devices_cuts_them_off(app):
    device = pair(app)
    phone = connect(f"{address(app)}?token={device}")
    app._command({"cmd": "forget_devices"})
    with pytest.raises(ConnectionClosed):
        for _ in range(50):
            phone.recv(timeout=5)
    with pytest.raises(ConnectionClosed):
        with connect(f"{address(app)}?token={device}") as again:
            again.recv(timeout=5)
    assert app.remote_info()["devices"] == 0


def test_a_source_that_keeps_guessing_is_refused_without_locking_out_others(app):
    app._command({"cmd": "pair_device"})
    code = app.remote_info()["code"]
    for _ in range(server.MAX_BAD_CODES):
        assert not app._allowed("WRONGCOD", remote=True, source="192.0.2.10")
    assert not app._allowed(code, remote=True, source="192.0.2.10")  # this source: refused for a while
    assert app._allowed(code, remote=True, source="192.0.2.11") == "paired"  # another: the right code works
    assert app._allowed("app-token", remote=False) and not app._allowed("app-token", remote=True, source="192.0.2.12")


def test_a_page_from_another_site_cannot_connect(app):
    device = pair(app)
    with pytest.raises(ConnectionClosed):
        with connect(f"{address(app)}?token={device}", origin="http://evil.example") as foreign:
            foreign.recv(timeout=5)


# ---- what a paired device may do ------------------------------------------------------------------------------


def test_a_paired_device_cannot_key_the_microphone_capture_keys_or_change_what_is_the_computers(app):
    device = pair(app)
    with connect(f"{address(app)}?token={device}") as phone:
        for command in ({"cmd": "ptt", "down": True}, {"cmd": "capture_ptt"}, {"cmd": "sound_check"}, {"cmd": "report"},
                        {"cmd": "save_log"}, {"cmd": "clear_joystick"}, {"cmd": "pair_device"}, {"cmd": "forget_devices"}):  # fmt: skip
            phone.send(json.dumps(command))
            assert "only be done in the app" in answers(phone, "error")["text"], command
        phone.send(json.dumps({"cmd": "set_key", "key": "sk-or-stolen"}))
        assert "only be changed in the app" in answers(phone, "error")["text"]
        changes = {
            "llm": {"provider": "local"},
            "ptt": {"key": "zzz"},
            "audio": {"input_device": 3},
            "atc": {"holding": "always"},
        }
        phone.send(json.dumps({"cmd": "settings", "changes": changes}))
        for _ in range(50):
            if app.session.config.atc.holding == "always":
                break
            threading.Event().wait(0.05)
    config = app.session.config
    assert config.atc.holding == "always"  # ATC settings: yes
    assert config.llm.provider == "openrouter" and config.ptt.key != "zzz" and config.audio.input_device != 3
    assert not app.session.ptt.pressed.is_set()


def test_a_paired_device_is_not_sent_the_simbrief_id_the_settings_or_file_paths(app):
    server.save_settings({"simbrief_user": "pilot-id-123"})
    app.emit({"type": "log_saved", "path": "C:/Users/someone/logs/flight.txt"})
    app.emit({"type": "info", "text": "hello phone"})
    state = server.build_state(app.session, None)  # what the state loop sends, once a second
    state["remote"] = app.remote_info()
    app._last_state, app._last_remote_state = json.dumps(state), json.dumps(server.for_remote(state))
    device = pair(app)
    received = []
    with connect(f"{address(app)}?token={device}") as phone:
        for _ in range(60):
            try:
                received.append(phone.recv(timeout=1.5))
            except TimeoutError:
                break
            if any('"type": "state"' in text for text in received):
                break
    text = "\n".join(received)
    assert "hello phone" in text
    assert "pilot-id-123" not in text and "log_saved" not in text and "Users/someone" not in text
    state = next(json.loads(t) for t in received if '"type": "state"' in t)
    for key in server.PRIVATE_STATE:
        assert key not in state


def test_a_paired_device_cannot_run_up_the_bill(app):
    device = pair(app)
    with connect(f"{address(app)}?token={device}") as phone:
        for _ in range(server.REMOTE_PAID_PER_MINUTE):
            phone.send(json.dumps({"cmd": "explain"}))
        phone.send(json.dumps({"cmd": "explain"}))
        assert "Too many requests" in answers(phone, "error", tries=60)["text"]


# ---- the computer's own window ------------------------------------------------------------------------------------


def test_a_window_that_goes_while_keying_the_microphone_lets_it_go(app):
    app.session.ready = True  # past start-up: the key is handled
    with _local(app) as window:
        window.send(json.dumps({"cmd": "ptt", "down": True}))
        for _ in range(50):
            if app.session.ptt.pressed.is_set():
                break
            threading.Event().wait(0.02)
        assert app.session.ptt.pressed.is_set()
    for _ in range(100):
        if not app.session.ptt.pressed.is_set():
            break
        threading.Event().wait(0.02)
    assert not app.session.ptt.pressed.is_set()


def _local(app):
    """The app's own listener, on a free port, as run() starts it."""
    from websockets.sync.server import serve

    listener = serve(app._handle, "127.0.0.1", 0)
    threading.Thread(target=listener.serve_forever, daemon=True).start()
    app._test_listener = listener
    return connect(f"ws://127.0.0.1:{listener.socket.getsockname()[1]}/?token=app-token")


def test_a_push_to_talk_key_the_listener_does_not_know_is_refused_before_it_is_saved():
    assert push_to_talk_key("ctrl_r") and push_to_talk_key("f13") and push_to_talk_key("v")
    for bad in ("zzz", "", "ctrl r", "x" * 40, "\n"):
        assert not push_to_talk_key(bad), bad
    with pytest.raises(ValueError):
        config_module.validate({"ptt": {"key": "zzz"}})


def test_a_key_with_a_line_break_is_not_written_into_the_env_file(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PROJECT_DIR", tmp_path)
    with pytest.raises(ValueError):
        server.save_key("sk-or-abc\nSIMBRIEF_USER=someone")
    assert not (tmp_path / ".env").exists()
    server.save_key("sk-or-abc")
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "OPENROUTER_API_KEY=sk-or-abc\n"


# ---- a second review ------------------------------------------------------------------------------------------


def test_reconnecting_does_not_start_a_devices_count_of_paid_calls_again(app):
    device = pair(app)
    counted = server._digest(device)
    for connection in range(2):  # the same device, two connections: the limit is the device's, not the connection's
        with connect(f"{address(app)}?token={device}") as phone:
            for _ in range(server.REMOTE_PAID_PER_MINUTE // 2):
                phone.send(json.dumps({"cmd": "explain"}))
            for _ in range(100):  # until the server has counted them
                if len(app._paid.get(counted, ())) == (connection + 1) * (server.REMOTE_PAID_PER_MINUTE // 2):
                    break
                threading.Event().wait(0.02)
    with connect(f"{address(app)}?token={device}") as phone:
        phone.send(json.dumps({"cmd": "explain"}))
        assert "Too many requests" in answers(phone, "error", tries=60)["text"]


def test_all_paired_devices_together_can_spend_only_so_much(app, monkeypatch):
    from collections import deque

    app._paid_all = deque(maxlen=3)
    monkeypatch.setattr(server, "REMOTE_PAID_PER_HOUR", 3)
    phones = [object() for _ in range(4)]
    for number, phone in enumerate(phones):
        app._device_of[phone] = f"device-{number}"  # four devices, one call each
    assert [app._paid_allowed(phone) for phone in phones] == [True, True, True, False]


def test_a_device_let_in_while_every_device_is_forgotten_is_cut_off_with_the_others(app, monkeypatch):
    # "Forget all" used to close only the connections already registered: one let in just before, and registered
    # just after, stayed connected. Being let in and being registered are now one step "Forget all" waits for.
    device = pair(app)
    allowed, forgotten, once = app._allowed, threading.Event(), []

    def let_in_then_forget(token, remote, source=""):
        granted = allowed(token, remote, source)
        if remote and not once:
            once.append(1)
            threading.Thread(target=lambda: (app.forget_devices(), forgotten.set()), daemon=True).start()
            forgotten.wait(1)  # forgotten here, between being let in and being registered, when nothing stops it
        return granted

    monkeypatch.setattr(app, "_allowed", let_in_then_forget)
    with pytest.raises(ConnectionClosed):
        with connect(f"{address(app)}?token={device}") as phone:
            for _ in range(200):
                phone.recv(timeout=5)
    assert forgotten.wait(5) and app.remote_info()["devices"] == 0


def test_a_paired_device_must_pair_again_after_a_month(app):
    import time

    month_ago = time.time() - (server.DEVICE_DAYS + 1) * 86400
    server.save_settings({"remote_devices": [{"hash": server._digest("old-device"), "paired": month_ago},
                                             {"hash": server._digest("new-device"), "paired": time.time()}]})  # fmt: skip
    assert app._allowed("old-device", remote=True, source="192.0.2.20") is False
    assert app._allowed("new-device", remote=True, source="192.0.2.21") == "device"
    assert app.remote_info()["devices"] == 1
    # A device paired by 0.21.0 (a hash alone) is let in, and its month counts from now.
    server.save_settings({"remote_devices": [server._digest("from-0.21")]})
    assert server._device_entries()[0]["hash"] == server._digest("from-0.21")
    assert app._allowed("from-0.21", remote=True, source="192.0.2.22") == "device"


# ---- a third review -------------------------------------------------------------------------------------------


def test_a_device_whose_month_runs_out_while_connected_is_cut_off(app):
    import time

    device = pair(app)
    with connect(f"{address(app)}?token={device}") as phone:
        for _ in range(50):
            if app._device_of:
                break
            threading.Event().wait(0.02)
        app.cut_off_unpaired()  # still paired: stays
        phone.send(json.dumps({"cmd": "clear_plan"}))
        expired = time.time() - (server.DEVICE_DAYS + 1) * 86400
        server.save_settings({"remote_devices": [{"hash": server._digest(device), "paired": expired}]})
        app.cut_off_unpaired()
        with pytest.raises(ConnectionClosed):
            for _ in range(200):
                phone.recv(timeout=5)


def test_a_settings_save_running_alongside_forget_all_does_not_bring_the_devices_back(app, monkeypatch):
    # A save reads the file, adds its change and writes it all back: if "Forget all" wrote in between, the save
    # put the forgotten devices back. Saves now take turns.
    pair(app)
    real = config_module.settings_path()
    read, go = threading.Event(), threading.Event()

    class Slow:
        def exists(self):
            return real.exists()

        def read_text(self, **kwargs):
            text = real.read_text(**kwargs)
            if threading.current_thread().name == "saver":
                read.set()
                go.wait(2)  # "Forget all" comes now
            return text

        def write_text(self, *args, **kwargs):
            return real.write_text(*args, **kwargs)

        def chmod(self, mode):
            pass

    monkeypatch.setattr(config_module, "settings_path", lambda: Slow())
    saver = threading.Thread(target=config_module.save_settings, args=({"simbrief_user": "someone"},), name="saver")
    saver.start()
    assert read.wait(2)
    forgetting = threading.Thread(target=app.forget_devices)
    forgetting.start()
    forgetting.join(0.5)
    go.set()
    saver.join(5)
    forgetting.join(5)
    assert server.paired_devices() == [] and server.load_settings()["simbrief_user"] == "someone"


# ---- a fourth review ------------------------------------------------------------------------------------------


def test_a_device_pairing_while_the_devices_are_checked_is_not_cut_off(app, monkeypatch):
    # The check read the list of paired devices, then looked at the connections: a device paired in between was
    # compared with the older list and cut off. Both now happen under the lock that lets a device in.
    app._command({"cmd": "pair_device"})
    code = app.remote_info()["code"]
    real, started = server.paired_devices, []

    def phone():
        try:
            with connect(f"{address(app)}?token={code}") as ws:
                while True:
                    ws.recv(timeout=3)
        except Exception:
            pass

    def list_then_pair():
        found = real()
        if threading.current_thread().name == "sweep" and not started:
            started.append(threading.Thread(target=phone, daemon=True))
            started[0].start()
            for _ in range(50):  # the device pairs now, if nothing stops it
                if app._device_of:
                    break
                threading.Event().wait(0.02)
        return found

    monkeypatch.setattr(server, "paired_devices", list_then_pair)
    sweep = threading.Thread(target=app.cut_off_unpaired, name="sweep")
    sweep.start()
    sweep.join(5)
    for _ in range(100):
        if app._device_of:
            break
        threading.Event().wait(0.02)
    threading.Event().wait(0.3)
    assert len(app._device_of) == 1  # paired, and still connected


def test_a_client_that_stops_reading_holds_up_nobody_else(app):
    class Stuck:
        socket = None

        def __init__(self):
            self.unstuck = threading.Event()

        def send(self, text):
            self.unstuck.wait(60)  # a client that never reads: the write never finishes

    class Reader:
        def __init__(self):
            self.got = []

        def send(self, text):
            self.got.append(text)

    stuck, reader = Stuck(), Reader()
    with app.lock:
        for client in (stuck, reader):
            app._outbox[client] = server.Outbox(client)
            app.clients.add(client)

    def send_all():
        for n in range(server.OUTBOX + 20):
            app._broadcast(f"m{n}")
            if n % 16 == 0:
                threading.Event().wait(0.01)  # a busy flight, not a flood: a client that reads keeps up

    sending = threading.Thread(target=send_all)
    sending.start()
    sending.join(10)
    stuck.unstuck.set()
    assert not sending.is_alive()  # the stuck client held up nothing
    for _ in range(100):
        if len(reader.got) == server.OUTBOX + 20:
            break
        threading.Event().wait(0.02)
    assert len(reader.got) == server.OUTBOX + 20
    assert stuck not in app.clients and reader in app.clients  # only the one too far behind is cut off


def test_a_device_whose_month_is_up_loses_its_commands_before_any_check_runs(app):
    import time

    device = pair(app)
    with connect(f"{address(app)}?token={device}") as phone:
        for _ in range(50):
            if app._expires:
                break
            threading.Event().wait(0.02)
        with app.lock:
            for connection in app._expires:
                app._expires[connection] = time.time() - 1
        before = app.session.config.atc.holding
        phone.send(json.dumps({"cmd": "settings", "changes": {"atc": {"holding": "always" if before != "always" else "never"}}}))
        with pytest.raises(ConnectionClosed):  # cut off, the error perhaps arriving first
            for _ in range(200):
                phone.recv(timeout=5)
    assert app.session.config.atc.holding == before  # and the command was not carried out
