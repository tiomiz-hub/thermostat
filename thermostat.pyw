"""Always-on-top desktop widget: Siemens Synco heating (local OZW772 gateway) + Samsung AC (SmartThings).

Double-click to run. Click the room name to switch rooms; right-click for options
(including the Room settings window with setpoints, weekly time switch and room state).
Settings live in %APPDATA%\\ThermostatWidget\\config.json; the password is encrypted
with Windows DPAPI so only your Windows account can decrypt it.
"""

import base64
import ctypes
import ctypes.wintypes
import html
import json
import os
import queue
import re
import ssl
import sys
import threading
import time
import colorsys
import hashlib
import subprocess
import unicodedata
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from html.parser import HTMLParser
from tkinter import ttk

# Raise this when you push a change: other PCs update when GitHub has a higher number.
__version__ = "1.0.0"

UPDATE_REPO = "tiomiz-hub/thermostat"
UPDATE_BRANCH = "main"

# --- Settings ---------------------------------------------------------------

DEFAULTS = {
    "host": "https://192.168.0.106",
    "user": "Tomas",
    "room_id": 3696,
    "room_name": "Tomo kambarys",
    "rooms_page": 3490,
    "rooms": [],           # cached [[name, page id], ...] from the gateway
    "dp_actual": 3718,     # Room state > Actual value room temp
    "dp_setpoint": 3703,   # Room setpoints > Comfort heating setpoint (written)
    "dp_current": 3719,    # Room state > Current room temp setpoint (what the room aims for now)
    "dp_mode": 3698,       # Room operating mode > Preselection (Auto/Comfort/...; written)
    "dp_mode_state": 3699,
    "dp_mode_cause": 3700,
    "info_room": None,     # room the dp_* ids above were looked up for
    "room_icons": {},      # optional {room name: image path}; otherwise icons/<room name>.png is used
    "panel_color": None,   # widget background picked from the menu (None = default dark)
    "step": 0.5,
    "refresh_seconds": 30,
    "save_delay_ms": 1000,
    "topmost": True,
    "alpha": 0.96,
    "collapsed": {},
    "x": None,
    "y": None,
}

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "ThermostatWidget")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
STARTUP_DIR = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")
STARTUP_FILE = os.path.join(STARTUP_DIR, "ThermostatWidget.cmd")


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    # A saved icon list would otherwise hide icons added to DEFAULTS later; saved entries still win.
    cfg["room_icons"] = {**DEFAULTS["room_icons"], **(cfg.get("room_icons") or {})}
    return cfg


ICONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
ICON_EXTS = (".png", ".gif")  # what tk.PhotoImage can open


def _icon_key(name):
    """'Gabrielės Cozy Place' -> 'gabrieles_cozy_place' (accents dropped, case and spacing ignored)."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", plain.lower()).strip("_")


def find_room_icon(room_name):
    """Image in the icons folder whose file name matches the room name, or None."""
    key = _icon_key(room_name or "")
    try:
        files = os.listdir(ICONS_DIR)
    except OSError:
        return None
    for fname in files:
        stem, ext = os.path.splitext(fname)
        if ext.lower() in ICON_EXTS and key and _icon_key(stem) == key:
            return os.path.join(ICONS_DIR, fname)
    return None


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


# --- Self-update from GitHub ------------------------------------------------
# On start the widget fetches thermostat.pyw from UPDATE_REPO. If its __version__ is higher it
# replaces this file (keeping thermostat.pyw.bak), syncs the icons folder and restarts.
# Offline or any error: it just carries on with the current version.
# Config keys: "auto_update" (default true), "update_token" (only needed for a private repo).

def _version_tuple(text):
    return tuple(int(p) for p in re.findall(r"\d+", text))


def _github_get(path, token, raw=True, timeout=5):
    url = f"https://api.github.com/repos/{UPDATE_REPO}/{path}"
    headers = {"Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
               "User-Agent": "ThermostatWidget-updater"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return r.read()


def _git_blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _sync_icons(token):
    """Download icons that are new or changed on GitHub (git's blob hash tells which)."""
    tree = json.loads(_github_get(f"git/trees/{UPDATE_BRANCH}?recursive=1", token, raw=False))
    for item in tree.get("tree", []):
        path = item.get("path", "")
        if item.get("type") != "blob" or not path.startswith("icons/") or ".." in path:
            continue
        local = os.path.join(os.path.dirname(ICONS_DIR), *path.split("/"))
        try:
            with open(local, "rb") as f:
                if _git_blob_sha(f.read()) == item.get("sha"):
                    continue
        except OSError:
            pass
        data = _github_get(f"contents/{urllib.parse.quote(path)}?ref={UPDATE_BRANCH}", token)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local + ".tmp", "wb") as f:
            f.write(data)
        os.replace(local + ".tmp", local)


def check_for_update(cfg):
    """Returns True if this file was replaced by a newer version (the caller should restart)."""
    if not cfg.get("auto_update", True):
        return False
    token = cfg.get("update_token")
    try:
        new = _github_get(f"contents/thermostat.pyw?ref={UPDATE_BRANCH}", token)
        m = re.search(rb'^__version__ = "([^"]+)"', new, re.M)
        if not m or _version_tuple(m.group(1).decode()) <= _version_tuple(__version__):
            return False
        compile(new, "thermostat.pyw", "exec")  # never install a broken download
        try:
            _sync_icons(token)
        except Exception:
            pass  # icons are a nice-to-have; the script update still goes ahead
        me = os.path.abspath(__file__)
        with open(me, "rb") as f:
            old = f.read()
        with open(me + ".bak", "wb") as f:
            f.write(old)
        with open(me + ".tmp", "wb") as f:
            f.write(new)
        os.replace(me + ".tmp", me)
        return True
    except Exception:
        return False


def restart():
    subprocess.Popen([sys.executable, os.path.abspath(__file__)] + sys.argv[1:],
                     cwd=os.path.dirname(os.path.abspath(__file__)))
    sys.exit(0)


# --- Windows DPAPI (password encryption tied to the Windows user) -----------

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _crypt(fn, data):
    blob_in, _keep = _blob(data)
    blob_out = _Blob()
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def protect(text):
    return base64.b64encode(_crypt(ctypes.windll.crypt32.CryptProtectData, text.encode("utf-8"))).decode()


def unprotect(token):
    return _crypt(ctypes.windll.crypt32.CryptUnprotectData, base64.b64decode(token)).decode("utf-8")


# --- Gateway client ---------------------------------------------------------

class AuthError(Exception):
    pass


class GatewayError(Exception):
    pass


class OfflineError(GatewayError):
    """The gateway could not be reached at all (network down, timeout, wrong address)."""


def _multipart(fields):
    boundary = "----tw" + uuid.uuid4().hex
    parts = []
    for name, value in fields:
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n")
    parts.append(f"--{boundary}--\r\n")
    return "".join(parts).encode("utf-8"), f"multipart/form-data; boundary={boundary}"


class _DialogFormParser(HTMLParser):
    """Collects the controls of the gateway's <form name="dialogform"> in document order."""

    def __init__(self):
        super().__init__()
        self.in_form = False
        self.controls = []
        self._select = None
        self._option = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and a.get("name") == "dialogform":
            self.in_form = True
        elif not self.in_form:
            return
        elif tag == "input":
            self.controls.append({"tag": "input", "name": a.get("name"), "type": (a.get("type") or "text").lower(),
                                  "value": a.get("value") or "", "checked": "checked" in a,
                                  "disabled": "disabled" in a})
        elif tag == "select":
            self._select = {"tag": "select", "name": a.get("name"), "disabled": "disabled" in a,
                            "options": [], "value": None}
            self.controls.append(self._select)
        elif tag == "option" and self._select is not None:
            self._option = [a.get("value"), "", "selected" in a]
            self._select["options"].append(self._option)

    def handle_data(self, data):
        if self._option is not None:
            self._option[1] += data

    def handle_endtag(self, tag):
        if tag == "option":
            self._option = None
        elif tag == "select" and self._select is not None:
            opts = self._select["options"]
            for o in opts:
                o[1] = o[1].strip()
            chosen = [o for o in opts if o[2]] or opts[:1]
            self._select["value"] = chosen[0][0] if chosen else ""
            self._select = None
        elif tag == "form":
            self.in_form = False


def _form_fields(controls, submit=None):
    """What a browser would post for these controls (disabled/unchecked/buttons left out)."""
    out = []
    for c in controls:
        if not c["name"] or c["disabled"]:
            continue
        if c["tag"] == "select":
            out.append((c["name"], c["value"]))
        elif c["type"] in ("button", "reset", "image"):
            continue
        elif c["type"] == "submit":
            if c["name"] == submit:
                out.append((c["name"], c["value"]))
        elif c["type"] in ("checkbox", "radio"):
            if c["checked"]:
                out.append((c["name"], c["value"] or "on"))
        else:
            out.append((c["name"], c["value"]))
    return out


_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def normalize_day(points, dedupe=True):
    """Mimic the site's Check: enabled points sorted by time, repeated modes dropped, then disabled ones."""
    enabled = []
    for t, mode, on in points:
        if not on:
            continue
        m = _TIME_RE.match(t.strip())
        if not m:
            raise ValueError(f"Invalid time {t!r} (use HH:MM)")
        enabled.append((f"{int(m.group(1)):02d}:{m.group(2)}", mode))
    enabled.sort(key=lambda p: p[0])
    kept = []
    for t, mode in enabled:
        if kept and kept[-1][0] == t:
            kept[-1] = (t, mode)  # same time twice: the later entry wins
        elif dedupe and kept and kept[-1][1] == mode:
            continue
        else:
            kept.append((t, mode))
    return kept


class Gateway:
    """Talks to the OZW772's own web pages (same calls the Siemens cloud proxies)."""

    def __init__(self, host, user, password, timeout=10):
        self.base = host.rstrip("/") + "/"
        self.user = user
        self.password = password
        self.timeout = timeout
        self.session_id = None
        # The gateway uses a self-signed certificate; we only ever talk to this one LAN host.
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE

    # -- transport / session --

    def _request(self, path, data=None, content_type=None):
        req = urllib.request.Request(self.base + path, data=data)
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except OSError as e:
            raise OfflineError(str(e)) from e

    def _sid(self):
        return urllib.parse.quote(self.session_id or "", safe="")

    def login(self):
        page = self._request("main.app")
        m = re.search(r'name="SessionId" value="([^"]+)"', page)
        if not m:
            raise GatewayError("Login page not recognised")
        self.session_id = m.group(1)
        body, ctype = _multipart([("user", self.user), ("pwd", self.password), ("login", "Login")])
        page = self._request(f"main.app?SessionId={self._sid()}&section=auth", body, ctype)
        if 'id="loginform"' in page:
            self.session_id = None
            raise AuthError("Wrong user name or password")
        m = re.search(r"SessionId=([0-9a-fA-F-]{20,})", page)
        if m:
            self.session_id = m.group(1)

    def _call(self, fn, *args):
        """Run fn with a session; log in again once if the gateway dropped it."""
        if not self.session_id:
            self.login()
        try:
            return fn(*args)
        except AuthError as e:
            if "Wrong" in str(e):
                raise
            self.login()
            return fn(*args)

    def _get(self, path):
        text = self._request(path.replace("{sid}", self._sid()))
        if 'id="loginform"' in text:
            raise AuthError("Session expired")
        return text

    # -- datapoint values --

    def _dp_once(self, dp_id):
        text = self._request(f"ajax.app?SessionId={self._sid()}&service=getDp&plantItemId={dp_id}")
        try:
            data = json.loads(text)
        except ValueError:
            raise AuthError("Session expired") if "loginform" in text else GatewayError("Unexpected reply")
        if data.get("service") != "getDp" or "value" not in data:
            raise AuthError("Session expired")
        return str(data["value"]).strip(), str(data.get("unit") or "").strip()

    def read_text(self, dp_id):
        """Value as the gateway shows it, plus its unit."""
        return self._call(self._dp_once, dp_id)

    def read(self, dp_id):
        value, _ = self.read_text(dp_id)
        m = re.search(r"-?\d+(?:[.,]\d+)?", value)
        if not m:
            raise GatewayError(f"No number in value {value!r}")
        return float(m.group(0).replace(",", "."))

    # -- menu pages --

    def _page_once(self, page_id):
        return self._get(f"main.app?SessionId={{sid}}&section=popcard&id={page_id}&idtype=4")

    def links(self, page_id):
        """[(name, id)] of the sub-pages linked from a menu page (navigation, not breadcrumb duplicates)."""
        page = self._call(self._page_once, page_id)
        seen, out = set(), []
        for pid, name in re.findall(r'section=popcard&amp;id=(\d+)&amp;idtype=4">([^<]+)</a>', page):
            pid, name = int(pid), html.unescape(name).strip()
            if pid in seen or pid == page_id:
                continue
            seen.add(pid)
            out.append((name, pid))
        return out

    def datapoints(self, page_id):
        """[(name, dp id, editable)] listed on a datapoint page."""
        page = self._call(self._page_once, page_id)
        out = []
        for row in page.split("<tr")[1:]:
            m_name = re.search(r'<td class="(dp_name(?:_edit)?)"[^>]*>(.*?)</td>', row, re.S)
            m_id = re.search(r'id="dp(\d+)"', row)
            if m_name and m_id:
                out.append((html.unescape(m_name.group(2)).strip(), int(m_id.group(1)),
                            m_name.group(1) == "dp_name_edit"))
        return out

    def rooms(self, rooms_page):
        return [(n, i) for n, i in self.links(rooms_page) if n != "Upward"]

    def room_info(self, room_id):
        """Sub-page ids of a room plus the datapoints the main widget shows."""
        pages = dict(self.links(room_id))
        info = {"pages": pages, "rooms_page": pages.get("Upward") or pages.get("Rooms")}
        state = self.datapoints(pages["Room state"])
        setpoints = [d for d in self.datapoints(pages["Room setpoints"]) if d[2]]
        info["actual"] = next((d[1] for d in state if "actual" in d[0].lower()), state[0][1])
        info["setpoint"] = next((d[1] for d in setpoints if "comfort" in d[0].lower()
                                 and "pre" not in d[0].lower()), setpoints[0][1])
        info["current"] = next((d[1] for d in state if "current" in d[0].lower()
                                and "setpoint" in d[0].lower()), None)
        # setpoint per mode name ("Comfort heating setpoint" -> "Comfort"), to match the mode State
        info["mode_setpoints"] = {}
        for name, dp, _ in setpoints:
            word = name.split()[0] if name else ""
            if word in BOOST_MODES:
                info["mode_setpoints"].setdefault(word, dp)
        modes = self.datapoints(pages["Room operating mode"])
        info["mode"] = next((d[1] for d in modes if "preselection" in d[0].lower()),
                            next(d[1] for d in modes if d[2]))
        info["mode_state"] = next((d[1] for d in modes if d[0].lower() == "state"), None)
        info["mode_cause"] = next((d[1] for d in modes if "cause" in d[0].lower()), None)
        return info

    # -- edit dialogs --

    def _form_once(self, dp_id):
        page = self._get(f"dialog.app?SessionId={{sid}}&action=new&id={dp_id}")
        parser = _DialogFormParser()
        parser.feed(page)
        if not parser.controls:
            raise GatewayError("Edit form not found")
        title = re.search(r'control-cell-title[^"]*">([^<]+)<', page)
        return parser.controls, (html.unescape(title.group(1)).strip() if title else "")

    def _post_once(self, fields):
        body, ctype = _multipart(fields)
        text = self._request(f"dialog.app?SessionId={self._sid()}", body, ctype)
        if 'id="loginform"' in text:
            raise AuthError("Session expired")

    def choice_form(self, dp_id):
        """A pick-one datapoint (e.g. operating mode): its options [(value, label)] and current value."""
        controls, title = self._call(self._form_once, dp_id)
        sel = next(c for c in controls if c["tag"] == "select")
        return {"title": title, "options": [(o[0], o[1]) for o in sel["options"]], "value": sel["value"]}

    def _write_choice_once(self, dp_id, value):
        controls, _ = self._form_once(dp_id)
        sel = next(c for c in controls if c["tag"] == "select")
        if value not in [o[0] for o in sel["options"]]:
            raise ValueError(f"Option {value!r} not offered by the gateway")
        sel["value"] = value
        self._post_once(_form_fields(controls))

    def write_choice(self, dp_id, value):
        return self._call(self._write_choice_once, dp_id, value)

    def number_form(self, dp_id):
        controls, title = self._call(self._form_once, dp_id)
        v = {c["name"]: c["value"] for c in controls if c["tag"] == "input"}
        res = float(v.get("res") or 0.1)
        return {"title": title, "min": float(v.get("min", "-1e9")), "max": float(v.get("max", "1e9")),
                "res": res, "decimals": int(v.get("decimals") or 1), "unit": v.get("unit", ""),
                "value": float(v.get("val") or 0)}

    def _write_once(self, dp_id, value):
        controls, _ = self._form_once(dp_id)
        v = {c["name"]: c["value"] for c in controls if c["tag"] == "input"}
        lo, hi = float(v.get("min", "-1e9")), float(v.get("max", "1e9"))
        res, decimals = float(v.get("res") or 0.1), int(v.get("decimals") or 1)
        value = min(max(round(round(value / res) * res, decimals), lo), hi)
        for c in controls:
            if c["name"] == "val":
                c["value"] = f"{value:.{decimals}f}"
        self._post_once(_form_fields(controls, submit="OK"))
        return value

    def write(self, dp_id, value):
        """Write a numeric setpoint (clamped to the device's limits and resolution)."""
        return self._call(self._write_once, dp_id, value)

    # -- weekly time switch --

    def read_schedule(self, dp_id):
        controls, _ = self._call(self._form_once, dp_id)
        by_name = {c["name"]: c for c in controls if c["name"]}
        nr_days = int(by_name["NrDays"]["value"])
        nr_points = int(by_name["NrSwitchPoints"]["value"])
        days = [o[1] for o in by_name["FromDay"]["options"]] if "FromDay" in by_name else \
            [f"Day {d + 1}" for d in range(nr_days)]
        modes = [(o[0], o[1]) for o in by_name["e_0_0"]["options"]]
        points = []
        for d in range(nr_days):
            day = []
            for p in range(nr_points):
                t, e, c = by_name[f"t_{d}_{p}"], by_name[f"e_{d}_{p}"], by_name[f"c_{d}_{p}"]
                day.append((t["value"], e["value"], c["checked"]))
            points.append(day)
        return {"days": days[:nr_days], "modes": modes, "points": points,
                "kind": int(by_name.get("Kind", {}).get("value") or 0)}

    def _write_schedule_once(self, dp_id, points):
        controls, _ = self._form_once(dp_id)
        by_name = {c["name"]: c for c in controls if c["name"]}
        nr_points = int(by_name["NrSwitchPoints"]["value"])
        dedupe = int(by_name.get("Kind", {}).get("value") or 0) != 4
        default_mode = by_name["e_0_0"]["options"][0][0]
        for d, day in enumerate(points):
            kept = normalize_day(day, dedupe)
            if len(kept) > nr_points:
                raise ValueError(f"Too many switch points on day {d + 1}")
            for p in range(nr_points):
                t, e, c = by_name[f"t_{d}_{p}"], by_name[f"e_{d}_{p}"], by_name[f"c_{d}_{p}"]
                on = p < len(kept)
                t["value"], e["value"] = kept[p] if on else ("00:00", default_mode)
                t["disabled"] = e["disabled"] = not on
                c["checked"] = on
        self._post_once(_form_fields(controls))

    def write_schedule(self, dp_id, points):
        """Save the whole week; points[day][i] = (HH:MM, mode value, enabled)."""
        return self._call(self._write_schedule_once, dp_id, points)


# --- Boost: hold a room at the widget's temperature -------------------------
# Synco has no "temporary override", so a boost fixes the room's operating mode and sets that
# mode's setpoint to the wanted value. The setpoints are chained (Comfort >= Precomfort >=
# Economy >= Protection, each one's minimum is the next one's value), so the mode is picked
# by which range the value fits. Every setpoint touched is remembered and put back on release.

BOOST_MODES = ("Comfort", "Precomfort", "Economy", "Protection")


def _boost_dps(gw, room_id):
    pages = dict(gw.links(room_id))
    setpoints = {}
    for name, dp, editable in gw.datapoints(pages["Room setpoints"]):
        word = name.split()[0] if name else ""
        if editable and word in BOOST_MODES and word not in setpoints:
            setpoints[word] = dp
    modes = gw.datapoints(pages["Room operating mode"])
    mode_dp = next((dp for n, dp, _ in modes if "preselection" in n.lower()), next(dp for _, dp, e in modes if e))
    return setpoints, mode_dp


def _choice_value(gw, mode_dp, label):
    return next(v for v, l in gw.choice_form(mode_dp)["options"] if l == label)


def boost_room(gw, room_id, target, boost=None):
    """Hold room_id at target. Returns the boost record (JSON-safe) needed to undo it later."""
    setpoints, mode_dp = _boost_dps(gw, room_id)
    if not boost or boost.get("room") != room_id:
        # Snapshot every chained setpoint at once: values captured at different moments can be
        # mutually impossible to restore (e.g. Comfort below Precomfort) if something else
        # changed a setpoint in between.
        boost = {"room": room_id, "originals": {str(dp): gw.read(dp) for dp in setpoints.values()}}
    usable = [m for m in BOOST_MODES if m in setpoints]
    for label in usable:
        form = gw.number_form(setpoints[label])
        if target >= form["min"] - 1e-6 or label == usable[-1]:
            break
    dp = setpoints[label]
    boost.update(mode_dp=mode_dp, mode=label, target=gw.write(dp, target))
    gw.write_choice(mode_dp, _choice_value(gw, mode_dp, label))
    return boost


def release_boost(gw, boost):
    """Back to the time switch (Auto) with every changed setpoint restored as far as the device allows.

    Returns {dp: (wanted, got)} for setpoints that could not be put back exactly (empty when all
    stuck). That is reported, not raised: the room is on its schedule either way, and retrying an
    impossible restore forever would leave the widget stuck in an error state.
    """
    gw.write_choice(boost["mode_dp"], _choice_value(gw, boost["mode_dp"], "Auto"))
    originals = {int(dp): value for dp, value in boost.get("originals", {}).items()}
    pending = {}
    for _ in range(len(originals) + 1):  # chained limits can clamp a write; repeat until all stick
        pending = {dp: v for dp, v in originals.items() if abs(gw.read(dp) - v) > 0.05}
        if not pending:
            return {}
        for dp, value in sorted(pending.items(), key=lambda kv: -kv[1]):
            gw.write(dp, value)
    return {dp: (v, gw.read(dp)) for dp, v in pending.items() if abs(gw.read(dp) - v) > 0.05}


# --- Samsung air conditioner (SmartThings cloud) ---------------------------
# Ported from C:\Users\Tiomiz\SamsungACRemote\ac_remote.pyw; it keeps using that
# widget's settings file so the existing SmartThings sign-in carries over.

ST_API = "https://api.smartthings.com/v1"
OAUTH_AUTHORIZE = "https://api.smartthings.com/oauth/authorize"
OAUTH_TOKEN = "https://api.smartthings.com/oauth/token"
OAUTH_SCOPE = "r:devices:* x:devices:*"
DEFAULT_REDIRECT = "https://httpbin.org/get"

AC_CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "SamsungACRemote")
AC_CONFIG_PATH = os.path.join(AC_CONFIG_DIR, "config.json")

AC_POLL_SECONDS = 30
AC_SETPOINT_DEBOUNCE_MS = 700

DEFAULT_AC_MODES = ["auto", "cool", "dry", "wind", "heat"]
DEFAULT_FAN_MODES = ["auto", "low", "medium", "high", "turbo"]
MODE_LABELS = {"auto": "Auto", "cool": "Cool", "dry": "Dry", "wind": "Fan", "fan": "Fan",
               "heat": "Heat", "aIComfort": "AI"}
FAN_LABELS = {"auto": "Auto", "low": "Low", "medium": "Med", "high": "High", "turbo": "Turbo"}


def ac_load_config():
    try:
        with open(AC_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def ac_save_config(cfg):
    os.makedirs(AC_CONFIG_DIR, exist_ok=True)
    tmp = AC_CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, AC_CONFIG_PATH)


class ApiError(Exception):
    pass


class SmartThingsClient:
    """Thin SmartThings REST client. Supports a Personal Access Token or an OAuth
    token pair (access + refresh) that is refreshed automatically."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()

    def _access_token(self):
        if self.cfg.get("auth") == "oauth":
            if time.time() > self.cfg.get("expires_at", 0) - 60:
                self.refresh_oauth()
            return self.cfg.get("access_token", "")
        return self.cfg.get("pat", "")

    def _token_request(self, form):
        cid, secret = self.cfg.get("client_id", ""), self.cfg.get("client_secret", "")
        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        req = urllib.request.Request(
            OAUTH_TOKEN,
            data=urllib.parse.urlencode(form).encode(),
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise ApiError(f"OAuth error {e.code}: {e.read().decode(errors='replace')[:200]}")
        except urllib.error.URLError as e:
            raise ApiError(f"Network error: {e.reason}")
        self.cfg["access_token"] = data["access_token"]
        # Each refresh returns a NEW refresh token; the old one stops working.
        self.cfg["refresh_token"] = data.get("refresh_token", self.cfg.get("refresh_token"))
        self.cfg["expires_at"] = time.time() + int(data.get("expires_in", 3600))
        ac_save_config(self.cfg)

    def exchange_code(self, code):
        self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.cfg.get("client_id", ""),
            "redirect_uri": self.cfg.get("redirect_uri", DEFAULT_REDIRECT),
        })

    def refresh_oauth(self):
        if not self.cfg.get("refresh_token"):
            raise ApiError("Not signed in. Right-click → AC settings… to connect.")
        self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": self.cfg["refresh_token"],
            "client_id": self.cfg.get("client_id", ""),
        })

    def _request(self, method, path, body=None, retry=True):
        with self.lock:
            token = self._access_token()
        if not token:
            raise ApiError("No token set. Right-click → AC settings…")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            ST_API + path, data=data, method=method,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json",
                     "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 401 and retry and self.cfg.get("auth") == "oauth":
                with self.lock:
                    self.refresh_oauth()
                return self._request(method, path, body, retry=False)
            if e.code == 401:
                raise ApiError("Token rejected (401). PATs expire after 24 h - "
                               "paste a new one or switch to OAuth.")
            raise ApiError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:200]}")
        except urllib.error.URLError as e:
            raise ApiError(f"Network error: {e.reason}")

    def list_air_conditioners(self):
        data = self._request("GET", "/devices")
        result = []
        for d in data.get("items", []):
            caps = {c["id"] for comp in d.get("components", []) for c in comp.get("capabilities", [])}
            if "airConditionerMode" in caps or "thermostatCoolingSetpoint" in caps:
                result.append((d["deviceId"], d.get("label") or d.get("name") or d["deviceId"]))
        return result

    def get_status(self, device_id):
        return self._request("GET", f"/devices/{device_id}/components/main/status")

    def command(self, device_id, capability, command, args=None):
        cmd = {"component": "main", "capability": capability, "command": command}
        if args is not None:
            cmd["arguments"] = args
        return self._request("POST", f"/devices/{device_id}/commands", {"commands": [cmd]})


def parse_status(status):
    """Turn the raw component status into a flat dict the UI can use."""

    def val(cap, attr, default=None):
        try:
            v = status[cap][attr]["value"]
            return default if v is None else v
        except (KeyError, TypeError):
            return default

    def unit(cap, attr):
        try:
            return status[cap][attr].get("unit") or "C"
        except (KeyError, TypeError, AttributeError):
            return "C"

    return {
        "power": val("switch", "switch", "off"),
        "mode": val("airConditionerMode", "airConditionerMode"),
        "modes": val("airConditionerMode", "supportedAcModes") or DEFAULT_AC_MODES,
        "fan": val("airConditionerFanMode", "fanMode"),
        "fans": val("airConditionerFanMode", "supportedAcFanModes") or DEFAULT_FAN_MODES,
        "setpoint": val("thermostatCoolingSetpoint", "coolingSetpoint"),
        "unit": unit("thermostatCoolingSetpoint", "coolingSetpoint"),
        "room": val("temperatureMeasurement", "temperature"),
    }


# --- Look (Samsung AC remote style) -----------------------------------------

DEFAULT_BG = "#1e2127"
BG = DEFAULT_BG
PANEL = "#272b33"
BORDER = "#3a3f4a"
FG = "#e6e6e6"
MUTED = "#8a909c"
ACCENT = "#3d8bfd"
ON_COLOR = "#2ecc71"
OFF_COLOR = "#e74c3c"
WARN = OFF_COLOR
HEAT = "#ff9f43"
DIM = MUTED


def _rgb(color):
    c = color.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _shift(color, *delta):
    return "#" + "".join(f"{min(255, max(0, v + d)):02x}" for v, d in zip(_rgb(color), delta))


def make_palette(bg):
    """Panel colours derived from one background colour (the default reproduces the Samsung look)."""
    bg = (bg or DEFAULT_BG).lower()
    if bg == DEFAULT_BG:
        return {"BG": bg, "PANEL": "#272b33", "BORDER": "#3a3f4a", "FG": "#e6e6e6", "MUTED": "#8a909c"}
    r, g, b = _rgb(bg)
    if (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.55:  # light colour: darker panels, dark text
        return {"BG": bg, "PANEL": _shift(bg, -16, -16, -16), "BORDER": _shift(bg, -45, -45, -45),
                "FG": "#1e2127", "MUTED": "#5b616c"}
    return {"BG": bg, "PANEL": _shift(bg, 9, 10, 12), "BORDER": _shift(bg, 28, 30, 35),
            "FG": "#e6e6e6", "MUTED": "#8a909c"}


def set_palette(bg):
    """Switch the module colours; returns {old colour: new colour} for recolouring live widgets."""
    global BG, PANEL, BORDER, FG, MUTED, DIM
    old = {"BG": BG, "PANEL": PANEL, "BORDER": BORDER, "FG": FG, "MUTED": MUTED}
    new = make_palette(bg)
    BG, PANEL, BORDER, FG, MUTED = new["BG"], new["PANEL"], new["BORDER"], new["FG"], new["MUTED"]
    DIM = MUTED
    return {old[k].lower(): new[k] for k in old}


_COLOR_OPTIONS = ("bg", "fg", "highlightbackground", "highlightcolor", "activebackground",
                  "activeforeground", "selectcolor", "insertbackground")


def safe_panel_color(bg):
    """Nudge bg by a shade if its palette would reuse a fixed colour (accent, heat, ...): the live
    recolouring maps colours by value, so a clash would repaint those widgets too."""
    reserved = {c.lower() for c in (ACCENT, ON_COLOR, OFF_COLOR, HEAT, "#ffffff", "#000000", "#666666")}
    bg = bg.lower()
    for _ in range(16):
        pal = make_palette(bg)
        if bg == DEFAULT_BG or (len(set(pal.values())) == 5 and not reserved & set(pal.values())):
            return bg
        r, g, b = _rgb(bg)
        bg = "#%02x%02x%02x" % (r, g, b + 1 if b < 255 else b - 1)
    return bg


def recolor(widget, mapping):
    """Apply {old colour: new colour} to a widget tree (only colours from the palette change)."""
    if getattr(widget, "_no_recolor", False):
        return
    for opt in _COLOR_OPTIONS:
        try:
            value = str(widget.cget(opt)).lower()
        except tk.TclError:
            continue
        if value in mapping:
            widget.config(**{opt: mapping[value]})
    if isinstance(widget, FlatButton):
        widget._bg = mapping.get(widget._bg.lower(), widget._bg)
        widget.config(bg=widget._bg)
    for child in widget.winfo_children():
        recolor(child, mapping)


class Tooltip:
    """Small hint shown under a widget while the mouse rests on it. Change .text any time."""

    DELAY_MS = 400

    def __init__(self, widget, text=""):
        self.widget, self.text = widget, text
        self.win = self.job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _=None):
        self._hide()
        self.job = self.widget.after(self.DELAY_MS, self._show)

    def _show(self):
        self.job = None
        if not self.text or not self.widget.winfo_exists():
            return
        self.win = tk.Toplevel(self.widget)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)  # above the widget even when it is always-on-top
        tk.Label(self.win, text=self.text, bg=PANEL, fg=FG, font=("Segoe UI", 8), justify="left",
                 padx=8, pady=4, highlightthickness=1, highlightbackground=BORDER).pack()
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        x = min(self.widget.winfo_rootx(), sw - w - 4)  # the widget usually sits at the screen edge
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        if y + h > sh:
            y = self.widget.winfo_rooty() - h - 4
        self.win.geometry(f"+{max(0, x)}+{y}")

    def _hide(self, _=None):
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None
        if self.win:
            self.win.destroy()
            self.win = None


class FlatButton(tk.Label):
    """Label-based button so colors render properly on Windows."""

    def __init__(self, master, text, command, bg=None, fg=None, font=("Segoe UI", 9), **kw):
        bg, fg = bg or PANEL, fg or FG  # resolved now: the palette can change at runtime
        super().__init__(master, text=text, bg=bg, fg=fg, font=font, cursor="hand2", **kw)
        self._bg = bg
        self._command = command
        self.bind("<Button-1>", lambda e: self._command())
        self.bind("<Enter>", lambda e: self.config(bg=self._hover()))
        self.bind("<Leave>", lambda e: self.config(bg=self._bg))

    def _hover(self):
        c = self._bg.lstrip("#")
        r, g, b = (min(255, int(c[i:i + 2], 16) + 25) for i in (0, 2, 4))
        return f"#{r:02x}{g:02x}{b:02x}"

    def set_colors(self, bg, fg=None):
        self._bg = bg
        self.config(bg=bg, fg=fg or FG)


def apply_dark_ttk(root):
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=FG, arrowcolor=FG)
    style.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", FG)],
              selectbackground=[("readonly", PANEL)], selectforeground=[("readonly", FG)])


# --- Widget -----------------------------------------------------------------

class Widget:
    """One always-on-top window: heating (Synco) on top, air conditioner (SmartThings) below."""

    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        set_palette(self.cfg.get("panel_color"))  # before any widget is built
        self.gw = None
        self.tasks = queue.Queue()
        self.target = None          # what the UI shows / will save
        self.saved_target = None    # last value confirmed by the gateway
        self.save_job = None
        self.refresh_job = None
        self.busy_saving = False
        self.settings_win = None
        self.color_win = None

        root.title("Climate")
        root.overrideredirect(True)
        root.attributes("-topmost", bool(self.cfg["topmost"]))
        root.attributes("-alpha", float(self.cfg["alpha"]))
        root.configure(bg=BG)
        apply_dark_ttk(root)
        self.topmost_var = tk.BooleanVar(value=bool(self.cfg["topmost"]))
        self.startup_var = tk.BooleanVar()
        self.alpha_var = tk.DoubleVar(value=float(self.cfg["alpha"]))
        if self.cfg["x"] is not None:
            root.geometry(f"+{self.cfg['x']}+{self.cfg['y']}")
        else:
            root.geometry(f"+{root.winfo_screenwidth() - 300}+60")

        self.outer = tk.Frame(root, bg=BG, padx=12, pady=10, highlightthickness=1, highlightbackground=BORDER)
        self.outer.pack(fill="both", expand=True)
        tk.Frame(self.outer, bg=BG, width=250, height=0).pack()  # keeps both sections the same width

        # heating section
        header = tk.Frame(self.outer, bg=BG)
        header.pack(fill="x")
        self.heat_dot = tk.Label(header, text="●", bg=BG, fg=MUTED, font=("Segoe UI", 10))
        self.heat_dot.pack(side="left")
        self.title = tk.Label(header, text=self.cfg["room_name"] + "  ▾", bg=BG, fg=FG,
                              font=("Segoe UI Semibold", 10), cursor="hand2")
        self.title.pack(side="left", padx=(4, 0))
        self.title.bind("<Button-1>", self._room_menu)
        self.heat_toggle = FlatButton(header, "▴", lambda: self.toggle_section("heat"), bg=BG, fg=MUTED, padx=4)
        self.heat_toggle.pack(side="right")
        tk.Label(header, text="HEATING", bg=BG, fg=MUTED, font=("Segoe UI", 7)).pack(side="right", padx=4)

        self.heat_body = tk.Frame(self.outer, bg=BG)
        temp = tk.Frame(self.heat_body, bg=BG)
        temp.pack(fill="x", pady=(8, 0))
        FlatButton(temp, "−", lambda: self.nudge(-1), font=("Segoe UI", 18), width=3).pack(side="left")
        mid = tk.Frame(temp, bg=BG)
        mid.pack(side="left", expand=True)
        num = tk.Frame(mid, bg=BG)
        num.pack()
        self.room_icon = tk.Label(num, bg=BG)  # optional picture per room (see "room_icons")
        self.setpoint = tk.Label(num, text="--°", bg=BG, fg=FG, font=("Segoe UI Light", 30))
        self.setpoint.pack(side="left")
        self._icons = {}
        self._update_room_icon(self.cfg["room_name"])
        self.actual = tk.Label(mid, text="room --°", bg=BG, fg=MUTED, font=("Segoe UI", 8))
        self.actual.pack()
        FlatButton(temp, "+", lambda: self.nudge(1), font=("Segoe UI", 18), width=3).pack(side="right")
        tk.Label(self.heat_body, text="MODE", bg=BG, fg=MUTED, font=("Segoe UI", 7)).pack(anchor="w", pady=(10, 2))
        self.mode_row = tk.Frame(self.heat_body, bg=BG)
        self.mode_row.pack(fill="x")
        self.mode_btns = {}
        self.mode_tips = {}
        self.status = tk.Label(self.heat_body, text="connecting…", bg=BG, fg=MUTED, font=("Segoe UI", 8),
                               wraplength=240, justify="left")
        self.status.pack(anchor="w", pady=(6, 0))
        self._build_mode_buttons()
        self.heat_header = header

        tk.Frame(self.outer, bg=BORDER, height=1).pack(fill="x", pady=10)

        self.ac = ACSection(self, self.outer)

        for w in (self.outer, header, self.heat_dot, self.heat_body, temp, mid, num, self.room_icon, self.setpoint,
                  self.actual, self.status, self.ac.header, self.ac.dot):
            w.bind("<ButtonPress-1>", self._drag_start, add="+")
            w.bind("<B1-Motion>", self._drag_move, add="+")
            w.bind("<ButtonRelease-1>", self._drag_end, add="+")
        root.bind_all("<Button-3>", self._menu)
        root.protocol("WM_DELETE_WINDOW", self.quit_app)          # Alt+F4
        root.protocol("WM_SAVE_YOURSELF", self._on_session_end)   # Windows shutdown / sign-out

        collapsed = self.cfg.get("collapsed", {})
        self._set_section("heat", not collapsed.get("heat"))
        self._set_section("ac", not collapsed.get("ac"))

        threading.Thread(target=self._worker, daemon=True).start()
        root.after(50, self._start)

    # -- sections --

    def toggle_section(self, key):
        shown = bool((self.heat_body if key == "heat" else self.ac.body).winfo_ismapped())
        self._set_section(key, not shown)
        self.cfg.setdefault("collapsed", {})[key] = shown
        save_config(self.cfg)

    def _set_section(self, key, show):
        body, header, toggle = ((self.heat_body, self.heat_header, self.heat_toggle) if key == "heat"
                                else (self.ac.body, self.ac.header, self.ac.toggle))
        if show:
            body.pack(fill="x", after=header)
        else:
            body.pack_forget()
        toggle.config(text="▴" if show else "▾")

    # -- background work: one thread, results handed back to Tk via after() --

    def _worker(self):
        while True:
            fn, done = self.tasks.get()
            try:
                result, err = fn(), None
            except Exception as e:  # report every failure to the UI
                result, err = None, e
            try:
                self.root.after(0, done, result, err)
            except RuntimeError:  # main loop already gone
                return

    def run(self, fn, done):
        self.tasks.put((fn, done))

    # -- login --

    def _start(self):
        token = self.cfg.get("password")
        if not token:
            self._ask_password()
            return
        try:
            password = unprotect(token)
        except OSError:
            self._ask_password("Saved password could not be read.")
            return
        self.gw = Gateway(self.cfg["host"], self.cfg["user"], password)
        self._after_login()

    def _after_login(self):
        if self.cfg.get("boost"):  # last session ended without restoring (crash, power cut, offline)
            self.status.config(text="Returning room to its schedule (left from last session)…", fg=MUTED)
            self._release_async()
        self.refresh()

    def _ask_password(self, message=""):
        win = tk.Toplevel(self.root, bg=BG)
        win.title("Thermostat login")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        body = tk.Frame(win, bg=BG, padx=14, pady=12)
        body.pack()
        lbl = dict(bg=BG, fg=FG, font=("Segoe UI", 9))
        ent = dict(bg=PANEL, fg=FG, insertbackground=FG, relief="flat", width=24)
        tk.Label(body, text=f"Thermostat gateway {self.cfg['host']}", **lbl).grid(row=0, column=0, columnspan=2, sticky="w")
        tk.Label(body, text="User", **lbl).grid(row=1, column=0, sticky="w", pady=(8, 0))
        user = tk.Entry(body, **ent)
        user.insert(0, self.cfg["user"])
        user.grid(row=1, column=1, pady=(8, 0), ipady=3)
        tk.Label(body, text="Password", **lbl).grid(row=2, column=0, sticky="w", pady=(4, 0))
        pwd = tk.Entry(body, show="•", **ent)
        pwd.grid(row=2, column=1, pady=(4, 0), ipady=3)
        info = tk.Label(body, text=message, bg=BG, fg=WARN, font=("Segoe UI", 8))
        info.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ok = FlatButton(body, "Log in", None, bg=ACCENT, fg="#ffffff", padx=16, pady=4)
        ok.grid(row=4, column=1, sticky="e", pady=(6, 0))
        busy = [False]

        def submit(_=None):
            if busy[0]:
                return
            u, p = user.get().strip(), pwd.get()
            gw = Gateway(self.cfg["host"], u, p)
            busy[0] = True
            info.config(text="Checking…", fg=MUTED)

            def done(_, err):
                busy[0] = False
                if not win.winfo_exists():
                    return
                if err:
                    info.config(text=str(err), fg=WARN)
                    return
                self.cfg["user"], self.cfg["password"] = u, protect(p)
                save_config(self.cfg)
                self.gw = gw
                win.destroy()
                self._after_login()

            self.run(gw.login, done)

        ok._command = submit
        pwd.bind("<Return>", submit)
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        pwd.focus_set()

    # -- rooms --

    def _room_menu(self, e):
        if not self.gw:
            return
        if self.cfg["rooms"]:
            self._show_room_menu(e.x_root, e.y_root)
            # refresh the cached list quietly so renamed/added rooms show up next time
            self.run(lambda: self.gw.rooms(self.cfg["rooms_page"]), self._on_rooms)
            return
        self.status.config(text="loading rooms…", fg=MUTED)
        x, y = e.x_root, e.y_root
        self.run(lambda: self.gw.rooms(self.cfg["rooms_page"]),
                 lambda r, err: (self._on_rooms(r, err), err or self._show_room_menu(x, y)))

    def _on_rooms(self, rooms, err):
        if err:
            self._show_error(err)
            return
        self.cfg["rooms"] = [list(r) for r in rooms]
        save_config(self.cfg)

    def _show_room_menu(self, x, y):
        m = self._new_menu()
        m.selected = tk.IntVar(m, self.cfg["room_id"])  # kept on the menu so it isn't garbage-collected
        for name, rid in self.cfg["rooms"]:
            m.add_radiobutton(label=name, value=rid, variable=m.selected,
                              command=lambda n=name, r=rid: self.select_room(n, r))
        m.tk_popup(x, y)

    def select_room(self, name, room_id):
        if self.save_job:  # don't send a pending change to the wrong room
            self.root.after_cancel(self.save_job)
            self.save_job = None
        self.title.config(text=name + "  ▾")
        self._update_room_icon(name)
        self.actual.config(text="room --°")
        self.target = None
        self.setpoint.config(text="--°", fg=FG)
        self._show_now(None)
        self._highlight_mode(None)
        self.status.config(text="loading room…", fg=MUTED)
        self._release_async()  # a boosted room goes back to its schedule before we leave it
        # Remember the choice right away (even if the widget is closed a moment later);
        # info_room=None makes the next refresh look up this room's datapoint ids.
        self.cfg.update(room_id=room_id, room_name=name, info_room=None)
        save_config(self.cfg)
        if self.settings_win and self.settings_win.winfo_exists():
            self.settings_win.destroy()
        self.refresh()

    # -- reading --

    def refresh(self):
        if self.refresh_job:
            self.root.after_cancel(self.refresh_job)
        self.refresh_job = self.root.after(self.cfg["refresh_seconds"] * 1000, self.refresh)
        if not self.gw:
            return
        gw, room, cfg = self.gw, self.cfg["room_id"], dict(self.cfg)

        def fetch():
            info = None
            if cfg.get("info_room") != room or not cfg.get("dp_mode_setpoints"):  # ids not looked up yet
                info = gw.room_info(room)
                cfg.update(self._info_cfg(room, info))
            text = lambda key: gw.read_text(cfg[key])[0] if cfg.get(key) else ""
            data = {"actual": gw.read(cfg["dp_actual"]), "mode": text("dp_mode"),
                    "state": text("dp_mode_state"), "cause": text("dp_mode_cause")}
            # The thermostat's own "Current room temp setpoint" lags minutes behind mode changes,
            # so the target shown is the setpoint of the mode the room is in right now. The
            # reported value (then the Comfort setpoint) is only a fallback for other states.
            mode_dp = cfg["dp_mode_setpoints"].get(data["state"])
            fallback = cfg.get("dp_current") or cfg["dp_setpoint"]
            data["current"] = gw.read(mode_dp or fallback)
            return room, info, data

        self.run(fetch, self._on_read)

    @staticmethod
    def _info_cfg(room, info):
        cfg = {"dp_actual": info["actual"], "dp_setpoint": info["setpoint"], "dp_current": info["current"],
               "dp_mode": info["mode"], "dp_mode_state": info["mode_state"],
               "dp_mode_cause": info["mode_cause"], "dp_mode_setpoints": info["mode_setpoints"],
               "info_room": room}
        if info.get("rooms_page"):
            cfg["rooms_page"] = info["rooms_page"]
        return cfg

    def _on_read(self, result, err):
        if err:
            self._show_error(err)
            return
        room, info, d = result
        if room != self.cfg["room_id"]:
            return  # answer for a room we already switched away from
        if info:
            self.cfg.update(self._info_cfg(room, info))
            save_config(self.cfg)
        self.heat_dot.config(fg=HEAT)
        boost = self._boost()
        if boost:
            aim, source = boost["target"], "Custom"
        else:
            aim = d["current"]
            source = "Schedule" if d["mode"] == "Auto" else f"{d['mode']} (fixed)"
        self.actual.config(text=f"{source} · room {d['actual']:.1f}°")
        self.saved_target = aim
        if self.save_job is None and not self.busy_saving:
            self.target = aim
            self._show_target()
        self._highlight_mode("Custom" if boost else ("Auto" if d["mode"] == "Auto" else None))
        self._show_now(d, boost)
        self.status.config(text="Updated " + time.strftime("%H:%M:%S"), fg=MUTED)

    # -- boost (hold the room at the widget's temperature until exit/shutdown) --

    def _boost(self):
        boost = self.cfg.get("boost")
        return boost if boost and boost.get("room") == self.cfg["room_id"] else None

    def _release_async(self):
        boost = self.cfg.get("boost")
        if not boost or not self.gw:
            return
        gw, record = self.gw, json.loads(json.dumps(boost))
        self.run(lambda: release_boost(gw, record), self._on_released)

    def _on_released(self, left, err):
        if err:
            self._show_error(err, prefix="Could not return room to schedule: ")
            return
        self.cfg.pop("boost", None)
        save_config(self.cfg)
        self._block_shutdown(False)
        if left:
            kept = ", ".join(f"{got:.1f}° (wanted {wanted:.1f}°)" for wanted, got in left.values())
            self.status.config(text=f"Back on schedule; a setpoint the thermostat wouldn't accept stays at {kept}",
                               fg=WARN)

    def _block_shutdown(self, on):
        """While a boost is active, ask Windows to give us a moment at shutdown (shows a reason if slow)."""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            if on:
                ctypes.windll.user32.ShutdownBlockReasonCreate(hwnd, ctypes.c_wchar_p("Returning heating to its schedule"))
            else:
                ctypes.windll.user32.ShutdownBlockReasonDestroy(hwnd)
        except Exception:
            pass

    def _on_session_end(self):
        """Windows is shutting down / signing out: restore synchronously, the process won't get another chance."""
        boost = self.cfg.get("boost")
        if not boost or not self.gw:
            return
        try:
            release_boost(Gateway(self.cfg["host"], self.cfg["user"], self.gw.password, timeout=5), boost)
            self.cfg.pop("boost", None)
            save_config(self.cfg)
        except Exception:
            pass  # stays recorded; restored on next start

    def quit_app(self):
        if getattr(self, "_closing", False):
            return
        self._closing = True
        if not (self.cfg.get("boost") and self.gw):
            self.root.destroy()
            return
        self.status.config(text="Returning room to its schedule…", fg=MUTED)
        gw, record = self.gw, json.loads(json.dumps(self.cfg["boost"]))

        def done(_, err):
            if not err:
                self.cfg.pop("boost", None)
                save_config(self.cfg)
            self._destroy_once()

        self.run(lambda: release_boost(gw, record), done)
        self.root.after(15000, self._destroy_once)  # gateway unreachable: give up, restore on next start

    def _destroy_once(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _update_room_icon(self, room_name):
        path = self.cfg.get("room_icons", {}).get(room_name)
        if path and not os.path.isabs(path):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
        if not path or not os.path.exists(path):
            path = find_room_icon(room_name)
        img = None
        if path:
            if path not in self._icons:
                try:
                    self._icons[path] = tk.PhotoImage(file=path)
                except tk.TclError:
                    self._icons[path] = None  # missing/unreadable file: just no icon
            img = self._icons[path]
        if img:
            self.room_icon.config(image=img)
            self.room_icon.pack(side="left", padx=(0, 6), before=self.setpoint)
        else:
            self.room_icon.pack_forget()

    # -- operating mode --

    # Auto = the room follows its time switch. Custom = the widget holds the room at the
    # temperature shown (a boost), until Auto is picked, the widget exits or Windows shuts down.

    def _build_mode_buttons(self):
        self.mode_btns = {}
        for i, label in enumerate(("Auto", "Custom")):
            b = FlatButton(self.mode_row, label, lambda l=label: self.set_mode(l), pady=4)
            b.grid(row=0, column=i, sticky="ew", padx=1)
            self.mode_row.columnconfigure(i, weight=1, uniform="m")
            self.mode_btns[label] = b
            self.mode_tips[label] = Tooltip(b)
        self._show_now(None)

    def _highlight_mode(self, label, color=ACCENT):
        for name, b in self.mode_btns.items():
            b.set_colors(color if name == label else PANEL)

    def _show_now(self, d, boost=None):
        """What the room is doing, shown as the tooltips of the Auto and Custom buttons."""
        auto, custom = "Follow the room's weekly schedule", "Hold the room at the temperature shown"
        if boost:
            custom = f"Holding {boost['target']:.1f}° (room fixed on {boost['mode']})"
            auto = "Return the room to its schedule"
        elif d and d["state"]:
            aim = f" · aiming {d['current']:.1f}°" if d["current"] is not None else ""
            if d["mode"] == "Auto":
                auto = f"Now {d['state']}{aim} ({(d['cause'] or 'time switch').lower()})"
            else:
                auto = (f"Now {d['state']}{aim}\nFixed on {d['mode']}: time switch not used\n"
                        "Click to return the room to its schedule")
        self.mode_tips["Auto"].text = auto
        self.mode_tips["Custom"].text = custom

    def set_mode(self, label):
        if not self.gw:
            return
        if self.save_job:
            self.root.after_cancel(self.save_job)
            self.save_job = None
        if label == "Custom":
            if self.target is not None:
                self._save(force=True)  # hold the temperature currently shown
            return
        self._highlight_mode("Auto", color=HEAT)  # orange until the gateway confirms
        self.status.config(text="Returning to schedule…", fg=MUTED)
        gw, dp = self.gw, self.cfg["dp_mode"]
        boost = self._boost()
        record = json.loads(json.dumps(boost)) if boost else None
        released = []

        def work():
            if record:  # ends the boost: setpoints restored, room back on Auto
                release_boost(gw, record)
                released.append(True)
            else:       # room was fixed on a mode by other means
                gw.write_choice(dp, _choice_value(gw, dp, "Auto"))

        def done(_, err):
            if released:
                self._on_released(None, None)
            if err:
                self._show_error(err, prefix="Save failed: ")
                return
            self.refresh()

        self.run(work, done)

    # -- writing --

    def nudge(self, direction):
        if self.target is None or not self.gw:
            return
        self.target = round(self.target + direction * self.cfg["step"], 1)
        self.target = min(max(self.target, 5.0), 35.0)
        self._show_target(pending=True)
        self._highlight_mode("Custom", color=HEAT)  # changing the temperature switches to Custom
        if self.save_job:
            self.root.after_cancel(self.save_job)
        self.save_job = self.root.after(self.cfg["save_delay_ms"], self._save)

    def _save(self, force=False):
        self.save_job = None
        if self.target == self.saved_target and not force:
            self._show_target()
            self.refresh()  # back where it was: show the real mode again
            return
        self._highlight_mode("Custom", color=HEAT)
        self.busy_saving = True
        self.status.config(text="Saving…", fg=MUTED)
        gw, room, value = self.gw, self.cfg["room_id"], self.target
        boost = self._boost()
        record = json.loads(json.dumps(boost)) if boost else None
        self.run(lambda: boost_room(gw, room, value, record), self._on_saved)

    def _on_saved(self, boost, err):
        self.busy_saving = False
        if err:
            self._show_error(err, prefix="Save failed: ")
            return
        self.cfg["boost"] = boost
        save_config(self.cfg)
        self._block_shutdown(True)
        self.saved_target = boost["target"]
        if self.save_job is None:
            self.target = boost["target"]
            self._show_target()
        self.status.config(text="Saved " + time.strftime("%H:%M:%S"), fg=MUTED)
        self.refresh()

    def _show_target(self, pending=False):
        self.setpoint.config(text=f"{self.target:.1f}°", fg=ACCENT if pending else FG)

    def _show_error(self, err, prefix=""):
        self.heat_dot.config(fg=OFF_COLOR)
        if isinstance(err, AuthError) and "Wrong" in str(err):
            self.gw = None
            self.status.config(text="Login failed", fg=WARN)
            self._ask_password(str(err))
            return
        if isinstance(err, OfflineError):
            text = f"Offline: thermostat gateway not reachable ({err})"
        else:
            text = f"{type(err).__name__}: {err}" if not isinstance(err, GatewayError) else f"Thermostat error: {err}"
        self.status.config(text=prefix + text, fg=WARN)

    # -- window handling --

    def _drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        dx, dy = self._drag
        self.root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def _drag_end(self, _):
        self.cfg["x"], self.cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
        save_config(self.cfg)

    def _new_menu(self):
        return tk.Menu(self.root, tearoff=0, bg=PANEL, fg=FG, activebackground=ACCENT,
                       activeforeground="#ffffff", selectcolor=FG, bd=0)

    def _menu(self, e):
        if e.widget.winfo_toplevel() is not self.root:
            return  # right-clicks inside settings/login windows
        m = self._new_menu()
        m.add_command(label="Room settings…", command=self.open_settings)
        m.add_command(label="AC settings…", command=self.ac.open_settings)
        m.add_command(label="Refresh now", command=lambda: (self.refresh(), self.ac.refresh()))
        m.add_separator()
        m.add_command(label="Change thermostat login…", command=self._ask_password)
        self.startup_var.set(os.path.exists(STARTUP_FILE))
        m.add_checkbutton(label="Start with Windows", variable=self.startup_var, command=self._toggle_startup)
        m.add_checkbutton(label="Always on top", variable=self.topmost_var, command=self._toggle_topmost)
        opacity = self._new_menu()
        for pct in (100, 96, 90, 80, 70, 60):
            opacity.add_radiobutton(label=f"{pct} %", value=pct / 100, variable=self.alpha_var,
                                    command=self._set_alpha)
        m.add_cascade(label="Opacity", menu=opacity)
        m.add_command(label="Panel color…", command=self.pick_panel_color)
        if self.cfg.get("panel_color"):
            m.add_command(label="Default panel color", command=lambda: self.set_panel_color(None))
        m.add_separator()
        m.add_command(label="Exit", command=self.quit_app)
        m.tk_popup(e.x_root, e.y_root)

    def open_settings(self):
        if not self.gw:
            return
        if self.settings_win and self.settings_win.winfo_exists():
            self.settings_win.lift()
            return
        self.settings_win = RoomSettings(self)

    def _toggle_startup(self):
        if not self.startup_var.get():
            if os.path.exists(STARTUP_FILE):
                os.remove(STARTUP_FILE)
            return
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        with open(STARTUP_FILE, "w", encoding="utf-8") as f:
            f.write(f'@start "" "{pythonw}" "{os.path.abspath(__file__)}"\r\n')

    def _toggle_topmost(self):
        self.cfg["topmost"] = self.topmost_var.get()
        save_config(self.cfg)
        self.root.attributes("-topmost", self.cfg["topmost"])
        for win in (self.settings_win, self.ac.settings):
            if win and win.winfo_exists():
                win.attributes("-topmost", self.cfg["topmost"])

    def pick_panel_color(self):
        if self.color_win and self.color_win.winfo_exists():
            self.color_win.lift()
            return
        self.color_win = PanelColorPicker(self)

    def set_panel_color(self, color, save=True):
        """Recolour the widget (and any open settings windows) and remember the choice."""
        if color:
            color = safe_panel_color(color)
        mapping = set_palette(color)
        recolor(self.root, mapping)
        apply_dark_ttk(self.root)
        self.cfg["panel_color"] = color
        if save:
            save_config(self.cfg)

    def _set_alpha(self):
        self.cfg["alpha"] = self.alpha_var.get()
        save_config(self.cfg)
        self.root.attributes("-alpha", self.cfg["alpha"])


class PanelColorPicker(tk.Toplevel):
    """Colour picker that recolours the widget live while you drag. Closing keeps the colour."""

    PRESETS = ("#1e2127", "#000000", "#2b2d42", "#1b263b", "#0b3d3a", "#1e3a2b", "#3b1f2b", "#4a2c17",
               "#5c5f66", "#9aa5b1", "#d9dde3", "#f4efe6", "#cfe3f5", "#d8f0dc", "#f6d6d6", "#efe0b9")

    def __init__(self, app):
        super().__init__(app.root)
        self._no_recolor = True  # keeps its own look while the widget around it changes
        self.app = app
        self.original = app.cfg.get("panel_color")
        self._syncing = False
        self._job = None
        self.title("Panel color")
        self.resizable(False, False)
        self.attributes("-topmost", app.cfg["topmost"])
        self.configure(bg=BG, padx=12, pady=10)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.undo())

        lbl = {"bg": BG, "fg": FG, "font": ("Segoe UI", 9)}
        grid = tk.Frame(self, bg=BG)
        grid.pack(anchor="w")
        for i, c in enumerate(self.PRESETS):
            sw = tk.Label(grid, bg=c, width=3, height=1, cursor="hand2",
                          highlightthickness=1, highlightbackground=BORDER)
            sw.grid(row=i // 8, column=i % 8, padx=2, pady=2)
            sw.bind("<Button-1>", lambda e, c=c: self.choose(c))

        self.h, self.s, self.v = tk.DoubleVar(), tk.DoubleVar(), tk.DoubleVar()
        for text, var, top in (("Hue", self.h, 360), ("Saturation", self.s, 100), ("Brightness", self.v, 100)):
            row = tk.Frame(self, bg=BG)
            row.pack(fill="x", pady=(6, 0))
            tk.Label(row, text=text, width=10, anchor="w", **lbl).pack(side="left")
            tk.Scale(row, from_=0, to=top, orient="horizontal", variable=var, showvalue=False, length=200,
                     command=lambda _: self._slid(), bg=BG, fg=FG, troughcolor=PANEL, highlightthickness=0,
                     activebackground=ACCENT, sliderrelief="flat", bd=0).pack(side="left")

        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", pady=(8, 0))
        self.preview = tk.Label(row, width=4, highlightthickness=1, highlightbackground=BORDER)
        self.preview.pack(side="left")
        self.hex = tk.StringVar()
        ent = tk.Entry(row, textvariable=self.hex, width=9, bg=PANEL, fg=FG, insertbackground=FG,
                       relief="flat", font=("Consolas", 10))
        ent.pack(side="left", padx=8, ipady=2)
        ent.bind("<Return>", lambda e: self._typed())
        ent.bind("<FocusOut>", lambda e: self._typed())

        btns = tk.Frame(self, bg=BG)
        btns.pack(fill="x", pady=(10, 0))
        FlatButton(btns, "Close", self.close, padx=10, pady=3).pack(side="right")
        FlatButton(btns, "Undo", self.undo, padx=10, pady=3).pack(side="right", padx=6)
        FlatButton(btns, "Default", lambda: self.choose(DEFAULT_BG), padx=10, pady=3).pack(side="right")

        self._show(self.original or DEFAULT_BG)
        self.update_idletasks()
        x = app.root.winfo_rootx() - self.winfo_reqwidth() - 8
        if x < 0:
            x = app.root.winfo_rootx() + app.root.winfo_width() + 8
        self.geometry(f"+{x}+{max(0, app.root.winfo_rooty())}")

    def _show(self, color):
        """Move the sliders, preview and text box to color without applying it."""
        self._syncing = True
        h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in _rgb(color)))
        self.h.set(h * 360)
        self.s.set(s * 100)
        self.v.set(v * 100)
        self._syncing = False
        self.preview.config(bg=color)
        self.hex.set(color)

    def _slid(self):
        if self._syncing:
            return
        r, g, b = colorsys.hsv_to_rgb(self.h.get() / 360, self.s.get() / 100, self.v.get() / 100)
        color = "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))
        self.preview.config(bg=color)
        self.hex.set(color)
        self._apply_soon(color)

    def _typed(self):
        text = self.hex.get().strip()
        if not text.startswith("#"):
            text = "#" + text
        if re.fullmatch(r"#[0-9a-fA-F]{6}", text) and text.lower() != (self.app.cfg.get("panel_color") or ""):
            self.choose(text.lower())

    def choose(self, color):
        self._show(color)
        self._apply_soon(color)

    def _apply_soon(self, color):
        # Dragging fires many events; recolour once per idle moment, not for each one.
        self._pending = color
        if not self._job:
            self._job = self.after(15, self._apply)

    def _apply(self):
        self._job = None
        color = None if self._pending == DEFAULT_BG else self._pending
        self.app.set_panel_color(color, save=False)

    def undo(self):
        self.choose(self.original or DEFAULT_BG)

    def close(self):
        if self._job:
            self.after_cancel(self._job)
            self._apply()
        save_config(self.app.cfg)
        self.destroy()


class ACSection:
    """The Samsung air conditioner part of the widget."""

    def __init__(self, app, parent):
        self.app = app
        self.root = app.root
        self.cfg = ac_load_config()
        self.client = SmartThingsClient(self.cfg)
        self.ac_state = {}
        self.pending_setpoint = None
        self.setpoint_job = None
        self.poll_job = None
        self.settings = None

        self.header = tk.Frame(parent, bg=BG)
        self.header.pack(fill="x")
        self.dot = tk.Label(self.header, text="●", bg=BG, fg=MUTED, font=("Segoe UI", 10))
        self.dot.pack(side="left")
        self.name_lbl = tk.Label(self.header, text=self._title(), bg=BG, fg=FG,
                                 font=("Segoe UI Semibold", 10), cursor="hand2")
        self.name_lbl.pack(side="left", padx=(4, 0))
        self.name_lbl.bind("<Button-1>", self._device_menu)
        self.toggle = FlatButton(self.header, "▴", lambda: app.toggle_section("ac"), bg=BG, fg=MUTED, padx=4)
        self.toggle.pack(side="right")
        tk.Label(self.header, text="AIR CON", bg=BG, fg=MUTED, font=("Segoe UI", 7)).pack(side="right", padx=4)

        self.body = tk.Frame(parent, bg=BG)
        self.power_btn = FlatButton(self.body, "⏻  Power", self.toggle_power, bg=PANEL,
                                    font=("Segoe UI Semibold", 11), pady=8)
        self.power_btn.pack(fill="x", pady=(10, 8))

        temp = tk.Frame(self.body, bg=BG)
        temp.pack(fill="x")
        FlatButton(temp, "−", lambda: self.change_setpoint(-1), font=("Segoe UI", 18), width=3).pack(side="left")
        mid = tk.Frame(temp, bg=BG)
        mid.pack(side="left", expand=True)
        self.setpoint_lbl = tk.Label(mid, text="--°", bg=BG, fg=FG, font=("Segoe UI Light", 30))
        self.setpoint_lbl.pack()
        self.room_lbl = tk.Label(mid, text="Room --°", bg=BG, fg=MUTED, font=("Segoe UI", 8))
        self.room_lbl.pack()
        FlatButton(temp, "+", lambda: self.change_setpoint(1), font=("Segoe UI", 18), width=3).pack(side="right")

        tk.Label(self.body, text="MODE", bg=BG, fg=MUTED, font=("Segoe UI", 7)).pack(anchor="w", pady=(10, 2))
        self.mode_row = tk.Frame(self.body, bg=BG)
        self.mode_row.pack(fill="x")
        tk.Label(self.body, text="FAN", bg=BG, fg=MUTED, font=("Segoe UI", 7)).pack(anchor="w", pady=(8, 2))
        self.fan_row = tk.Frame(self.body, bg=BG)
        self.fan_row.pack(fill="x")

        self.status_lbl = tk.Label(self.body, text="", bg=BG, fg=MUTED, font=("Segoe UI", 8),
                                   wraplength=240, justify="left")
        self.status_lbl.pack(anchor="w", pady=(10, 0))

        self.mode_btns = {}
        self.fan_btns = {}
        self._rebuild_choice_rows(DEFAULT_AC_MODES, DEFAULT_FAN_MODES)
        self.root.after(100, self.refresh)

    def _configured(self):
        has_auth = self.cfg.get("pat") or self.cfg.get("refresh_token")
        return bool(has_auth and self.cfg.get("device_id"))

    def _title(self):
        return self.cfg.get("device_name", "Air conditioner") + "  ▾"

    # ---- choosing the air conditioner

    def _device_menu(self, e):
        if not (self.cfg.get("pat") or self.cfg.get("refresh_token")):
            self.open_settings()
            return
        x, y = e.x_root, e.y_root
        if self.cfg.get("devices"):
            self._show_device_menu(x, y)
            # refresh the cached list quietly so newly added units show up next time
            self._run(self.client.list_air_conditioners, self._store_devices, busy=self.status_lbl.cget("text"))
            return
        self._run(self.client.list_air_conditioners,
                  lambda devs: (self._store_devices(devs), self._show_device_menu(x, y)),
                  busy="Looking for air conditioners…")

    def _store_devices(self, devs):
        self.cfg["devices"] = [list(d) for d in devs]
        ac_save_config(self.cfg)
        if self.status_lbl.cget("text") == "Looking for air conditioners…":
            self.set_status(f"Found {len(devs)} air conditioner(s).")

    def _show_device_menu(self, x, y):
        m = self.app._new_menu()
        m.selected = tk.StringVar(m, self.cfg.get("device_id", ""))  # kept on the menu so it isn't garbage-collected
        for dev_id, name in self.cfg.get("devices", []):
            m.add_radiobutton(label=name, value=dev_id, variable=m.selected,
                              command=lambda d=dev_id, n=name: self.select_device(d, n))
        if not self.cfg.get("devices"):
            m.add_command(label="No air conditioners found", state="disabled")
        m.tk_popup(x, y)

    def select_device(self, dev_id, name):
        if self.setpoint_job:  # don't send a pending change to the wrong unit
            self.root.after_cancel(self.setpoint_job)
            self.setpoint_job = None
        self.pending_setpoint = None
        self.cfg["device_id"], self.cfg["device_name"] = dev_id, name
        ac_save_config(self.cfg)
        self.ac_state = {}
        self.name_lbl.config(text=self._title())
        self.dot.config(fg=MUTED)
        self.power_btn.set_colors(PANEL)
        self.power_btn.config(text="⏻  Power")
        self.setpoint_lbl.config(text="--°")
        self.room_lbl.config(text="Room --°")
        for b in list(self.mode_btns.values()) + list(self.fan_btns.values()):
            b.set_colors(PANEL)
        self.refresh()

    def _rebuild_choice_rows(self, modes, fans):
        if list(self.mode_btns) == list(modes) and list(self.fan_btns) == list(fans):
            return
        for row, items, store, labels, cap, cmd in (
                (self.mode_row, modes, self.mode_btns, MODE_LABELS, "airConditionerMode", "setAirConditionerMode"),
                (self.fan_row, fans, self.fan_btns, FAN_LABELS, "airConditionerFanMode", "setFanMode")):
            for w in row.winfo_children():
                w.destroy()
            store.clear()
            for i, m in enumerate(items):
                b = FlatButton(row, labels.get(m, m.capitalize()),
                               lambda m=m, cap=cap, cmd=cmd: self.send(cap, cmd, [m]), pady=4)
                b.grid(row=0, column=i, sticky="ew", padx=1)
                row.columnconfigure(i, weight=1, uniform="c")
                store[m] = b

    # ---- background work

    def _run(self, fn, on_ok=None, busy="Working…"):
        self.set_status(busy)

        def worker():
            try:
                result = fn()
            except ApiError as e:
                self.root.after(0, lambda: self._on_error(str(e)))
            except Exception as e:  # keep the widget alive on unexpected errors
                self.root.after(0, lambda: self._on_error(f"{type(e).__name__}: {e}"))
            else:
                self.root.after(0, lambda: on_ok(result) if on_ok else None)

        threading.Thread(target=worker, daemon=True).start()

    def _on_error(self, msg):
        self.dot.config(fg=OFF_COLOR)
        self.set_status(msg)

    def set_status(self, text):
        self.status_lbl.config(text=text)

    # ---- actions

    def refresh(self, schedule=True):
        if schedule:
            if self.poll_job:
                self.root.after_cancel(self.poll_job)
            self.poll_job = self.root.after(AC_POLL_SECONDS * 1000, self.refresh)
        if not self._configured():
            self.set_status("Not set up yet - right-click → AC settings…")
            return
        dev = self.cfg["device_id"]
        self._run(lambda: (dev, parse_status(self.client.get_status(dev))), self._apply_state, busy="Updating…")

    def _apply_state(self, result):
        dev, st = result
        if dev != self.cfg.get("device_id"):
            return  # answer for a unit we already switched away from
        self.ac_state = st
        self.dot.config(fg=ACCENT)
        on = st["power"] == "on"
        self.power_btn.set_colors(ON_COLOR if on else PANEL, "#ffffff" if on else FG)
        self.power_btn.config(text="⏻  On" if on else "⏻  Off")
        u = "°F" if st["unit"] == "F" else "°"
        sp = self.pending_setpoint if self.pending_setpoint is not None else st["setpoint"]
        self.setpoint_lbl.config(text=f"{sp:g}{u}" if sp is not None else f"--{u}")
        self.room_lbl.config(text=f"Room {st['room']:g}{u}" if st["room"] is not None else "Room --")
        self._rebuild_choice_rows(st["modes"], st["fans"])
        for m, b in self.mode_btns.items():
            b.set_colors(ACCENT if m == st["mode"] else PANEL)
        for m, b in self.fan_btns.items():
            b.set_colors(ACCENT if m == st["fan"] else PANEL)
        self.set_status(time.strftime("Updated %H:%M:%S"))

    def send(self, capability, command, args=None):
        if not self._configured():
            self.open_settings()
            return
        dev = self.cfg["device_id"]

        def do():
            self.client.command(dev, capability, command, args)
            time.sleep(1.5)  # give the AC a moment before reading back the state
            return dev, parse_status(self.client.get_status(dev))

        self._run(do, self._apply_state, busy="Sending…")

    def toggle_power(self):
        on = self.ac_state.get("power") == "on"
        self.send("switch", "off" if on else "on")

    def change_setpoint(self, delta):
        base = self.pending_setpoint if self.pending_setpoint is not None else self.ac_state.get("setpoint")
        if base is None:
            return
        lo, hi = (60, 86) if self.ac_state.get("unit") == "F" else (16, 30)
        self.pending_setpoint = max(lo, min(hi, base + delta))
        u = "°F" if self.ac_state.get("unit") == "F" else "°"
        self.setpoint_lbl.config(text=f"{self.pending_setpoint:g}{u}")
        if self.setpoint_job:
            self.root.after_cancel(self.setpoint_job)
        self.setpoint_job = self.root.after(AC_SETPOINT_DEBOUNCE_MS, self._commit_setpoint)

    def _commit_setpoint(self):
        self.setpoint_job = None
        value = self.pending_setpoint
        self.pending_setpoint = None
        self.send("thermostatCoolingSetpoint", "setCoolingSetpoint", [value])

    def open_settings(self):
        if self.settings and self.settings.winfo_exists():
            self.settings.lift()
            return
        self.settings = ACSettingsDialog(self)


class ACSettingsDialog(tk.Toplevel):
    def __init__(self, section):
        super().__init__(section.root)
        self.app = section
        cfg = section.cfg
        self.title("Air conditioner settings")
        self.configure(bg=BG, padx=14, pady=12)
        self.attributes("-topmost", bool(section.app.cfg["topmost"]))
        self.resizable(False, False)

        self.auth = tk.StringVar(value=cfg.get("auth", "pat"))
        self.pat = tk.StringVar(value=cfg.get("pat", ""))
        self.client_id = tk.StringVar(value=cfg.get("client_id", ""))
        self.client_secret = tk.StringVar(value=cfg.get("client_secret", ""))
        self.redirect = tk.StringVar(value=cfg.get("redirect_uri", DEFAULT_REDIRECT))
        self.code = tk.StringVar()
        self.devices = []

        def label(parent, text, small=False):
            tk.Label(parent, text=text, bg=BG, fg=MUTED if small else FG, justify="left",
                     font=("Segoe UI", 8 if small else 9), wraplength=380).pack(anchor="w", pady=(6, 1))

        def entry(parent, var, show=None):
            e = tk.Entry(parent, textvariable=var, bg=PANEL, fg=FG, insertbackground=FG,
                         relief="flat", width=52, show=show)
            e.pack(fill="x", ipady=3)
            return e

        tk.Label(self, text="1. Sign-in method", bg=BG, fg=FG,
                 font=("Segoe UI Semibold", 10)).pack(anchor="w")
        radios = tk.Frame(self, bg=BG)
        radios.pack(anchor="w")
        for value, text in (("pat", "Personal access token (simple, expires every 24 h)"),
                            ("oauth", "OAuth app (one-time setup, stays signed in)")):
            tk.Radiobutton(radios, text=text, value=value, variable=self.auth, command=self._toggle,
                           bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG,
                           activeforeground=FG).pack(anchor="w")

        self.pat_frame = tk.Frame(self, bg=BG)
        label(self.pat_frame, "Create a token at account.smartthings.com/tokens with the "
                              "Devices: List, See and Control scopes, then paste it here.", small=True)
        entry(self.pat_frame, self.pat, show="•")
        FlatButton(self.pat_frame, "Open token page", lambda: webbrowser.open(
            "https://account.smartthings.com/tokens"), pady=3).pack(anchor="w", pady=4)

        self.oauth_frame = tk.Frame(self, bg=BG)
        label(self.oauth_frame, "Create an OAuth-In app once with the SmartThings CLI (see the "
                                "SamsungACRemote README), then enter its client ID and secret.", small=True)
        label(self.oauth_frame, "Client ID")
        entry(self.oauth_frame, self.client_id)
        label(self.oauth_frame, "Client secret")
        entry(self.oauth_frame, self.client_secret, show="•")
        label(self.oauth_frame, "Redirect URI (must match the app)")
        entry(self.oauth_frame, self.redirect)
        FlatButton(self.oauth_frame, "Open Samsung sign-in page", self._open_authorize,
                   pady=3).pack(anchor="w", pady=4)
        label(self.oauth_frame, "After approving, paste the code (or the whole URL you landed on):",
              small=True)
        entry(self.oauth_frame, self.code)
        self.oauth_state = tk.Label(self.oauth_frame, bg=BG, fg=MUTED, font=("Segoe UI", 8),
                                    text="Signed in ✓" if cfg.get("refresh_token") else "Not signed in")
        self.oauth_state.pack(anchor="w", pady=2)
        FlatButton(self.oauth_frame, "Connect", self._connect_oauth, pady=3).pack(anchor="w")

        self.sep = tk.Label(self, text="2. Air conditioner", bg=BG, fg=FG,
                            font=("Segoe UI Semibold", 10))
        self.dev_frame = tk.Frame(self, bg=BG)
        self.dev_combo = ttk.Combobox(self.dev_frame, state="readonly", width=40)
        current = cfg.get("device_name")
        if current:
            self.dev_combo.set(current)
        self.dev_combo.pack(side="left", fill="x", expand=True)
        FlatButton(self.dev_frame, "Find devices", self._load_devices, padx=8, pady=3).pack(
            side="left", padx=(6, 0))

        self.bottom = tk.Frame(self, bg=BG)
        self.msg = tk.Label(self.bottom, text="", bg=BG, fg=MUTED, font=("Segoe UI", 8),
                            wraplength=380, justify="left")
        self.msg.pack(anchor="w", pady=4)
        btns = tk.Frame(self.bottom, bg=BG)
        btns.pack(fill="x")
        FlatButton(btns, "Save", self._save, bg=ACCENT, fg="#ffffff", padx=16, pady=4).pack(side="right")
        FlatButton(btns, "Cancel", self.destroy, padx=12, pady=4).pack(side="right", padx=6)

        self._toggle()

    def _toggle(self):
        for w in (self.pat_frame, self.oauth_frame, self.sep, self.dev_frame, self.bottom):
            w.pack_forget()
        (self.pat_frame if self.auth.get() == "pat" else self.oauth_frame).pack(fill="x")
        self.sep.pack(anchor="w", pady=(14, 2))
        self.dev_frame.pack(fill="x")
        self.bottom.pack(fill="x", pady=(12, 0))

    def _apply_auth_fields(self):
        cfg = self.app.cfg
        cfg["auth"] = self.auth.get()
        cfg["pat"] = self.pat.get().strip()
        cfg["client_id"] = self.client_id.get().strip()
        cfg["client_secret"] = self.client_secret.get().strip()
        cfg["redirect_uri"] = self.redirect.get().strip() or DEFAULT_REDIRECT

    def _open_authorize(self):
        self._apply_auth_fields()
        cfg = self.app.cfg
        if not cfg["client_id"]:
            self.msg.config(text="Enter the client ID first.")
            return
        q = urllib.parse.urlencode({"client_id": cfg["client_id"], "response_type": "code",
                                    "redirect_uri": cfg["redirect_uri"], "scope": OAUTH_SCOPE})
        webbrowser.open(f"{OAUTH_AUTHORIZE}?{q}")

    def _connect_oauth(self):
        self._apply_auth_fields()
        raw = self.code.get().strip()
        if "code=" in raw:
            raw = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query).get("code", [""])[0]
        if not raw:
            self.msg.config(text="Paste the code first.")
            return
        self.msg.config(text="Connecting…")

        def worker():
            try:
                self.app.client.exchange_code(raw)
                self.after(0, lambda: (self.oauth_state.config(text="Signed in ✓"),
                                       self.msg.config(text="Signed in. Now click Find devices.")))
            except ApiError as e:
                self.after(0, lambda: self.msg.config(text=str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _load_devices(self):
        self._apply_auth_fields()
        self.msg.config(text="Looking for air conditioners…")

        def worker():
            try:
                devs = self.app.client.list_air_conditioners()
            except ApiError as e:
                self.after(0, lambda: self.msg.config(text=str(e)))
                return
            self.after(0, lambda: self._show_devices(devs))

        threading.Thread(target=worker, daemon=True).start()

    def _show_devices(self, devs):
        self.devices = devs
        self.dev_combo["values"] = [name for _, name in devs]
        if devs:
            self.dev_combo.current(0)
            self.msg.config(text=f"Found {len(devs)} air conditioner(s).")
        else:
            self.msg.config(text="No air conditioners found on this account.")

    def _save(self):
        self._apply_auth_fields()
        cfg = self.app.cfg
        idx = self.dev_combo.current()
        if idx >= 0 and self.devices:
            cfg["device_id"], cfg["device_name"] = self.devices[idx]
        if self.devices:
            cfg["devices"] = [list(d) for d in self.devices]
        ac_save_config(cfg)
        self.app.name_lbl.config(text=self.app._title())
        self.destroy()
        self.app.refresh()


# --- Room settings window ---------------------------------------------------

class RoomSettings(tk.Toplevel):
    """Setpoints, weekly time switch and room state for the widget's current room."""

    STATE_REFRESH_MS = 10000

    def __init__(self, widget):
        super().__init__(widget.root)
        self.w = widget
        self.gw = widget.gw
        self.room_id = widget.cfg["room_id"]
        self.title(f"{widget.cfg['room_name']} – room settings")
        self.attributes("-topmost", bool(widget.cfg["topmost"]))
        self.resizable(False, False)

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(padx=8, pady=8)
        self.sp_tab = ttk.Frame(self.tabs, padding=10)
        self.ts_tab = ttk.Frame(self.tabs, padding=10)
        self.st_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.sp_tab, text="Setpoints")
        self.tabs.add(self.ts_tab, text="Time switch")
        self.tabs.add(self.st_tab, text="Room state")
        for tab in (self.sp_tab, self.ts_tab, self.st_tab):
            ttk.Label(tab, text="Loading…").grid(row=0, column=0)

        self.pages = None
        widget.run(lambda: self.gw.room_info(self.room_id)["pages"], self._on_pages)

    def _alive(self):
        return self.winfo_exists()

    def _clear(self, tab):
        for child in tab.winfo_children():
            child.destroy()

    def _on_pages(self, pages, err):
        if not self._alive():
            return
        if err:
            for tab in (self.sp_tab, self.ts_tab, self.st_tab):
                self._clear(tab)
                ttk.Label(tab, text=f"Could not load: {err}").grid(row=0, column=0)
            return
        self.pages = pages
        self.load_setpoints()
        self.load_schedule()
        self.load_state()

    # -- Setpoints tab --

    def load_setpoints(self):
        gw, page = self.gw, self.pages["Room setpoints"]

        def fetch():
            return [(dp, gw.number_form(dp), name) for name, dp, editable in gw.datapoints(page) if editable]

        self.w.run(fetch, self._show_setpoints)

    def _show_setpoints(self, items, err):
        if not self._alive():
            return
        tab = self.sp_tab
        self._clear(tab)
        if err:
            ttk.Label(tab, text=f"Could not load: {err}").grid(row=0, column=0)
            return
        self.sp_vars = []
        for row, (dp, form, name) in enumerate(items):
            fmt = f"%.{form['decimals']}f"
            var = tk.StringVar(value=fmt % form["value"])
            ttk.Label(tab, text=form["title"] or name).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Spinbox(tab, from_=form["min"], to=form["max"], increment=form["res"], format=fmt,
                        textvariable=var, width=7, justify="right").grid(row=row, column=1, padx=8)
            ttk.Label(tab, text=f"{form['unit']}   ({form['min']:g} … {form['max']:g})",
                      foreground="#666").grid(row=row, column=2, sticky="w")
            self.sp_vars.append((dp, var, form))
        bar = ttk.Frame(tab)
        bar.grid(row=len(items), column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.sp_status = ttk.Label(bar, text="")
        self.sp_status.pack(side="left")
        ttk.Button(bar, text="Save", command=self.save_setpoints).pack(side="right")
        ttk.Button(bar, text="Reload", command=self.load_setpoints).pack(side="right", padx=6)

    def save_setpoints(self):
        changes = []
        for dp, var, form in self.sp_vars:
            try:
                value = float(var.get().replace(",", "."))
            except ValueError:
                self.sp_status.config(text=f"Not a number: {var.get()}", foreground=WARN)
                return
            if abs(value - form["value"]) > 1e-9:
                changes.append((dp, value))
        if not changes:
            self.sp_status.config(text="Nothing changed", foreground="#666")
            return
        self.sp_status.config(text="Saving…", foreground="#666")
        gw = self.gw

        def done(_, err):
            if not self._alive():
                return
            if err:
                self.sp_status.config(text=f"Save failed: {err}", foreground=WARN)
                return
            self.load_setpoints()
            self.w.refresh()

        self.w.run(lambda: [gw.write(dp, v) for dp, v in changes], done)

    # -- Time switch tab --

    def load_schedule(self):
        gw, page = self.gw, self.pages["Room time switch"]

        def fetch():
            dps = gw.datapoints(page)
            return dps[0][1], gw.read_schedule(dps[0][1])

        self.w.run(fetch, self._show_schedule)

    def _show_schedule(self, result, err):
        if not self._alive():
            return
        tab = self.ts_tab
        self._clear(tab)
        if err:
            ttk.Label(tab, text=f"Could not load: {err}").grid(row=0, column=0)
            return
        self.ts_dp, sched = result
        self.ts_modes = sched["modes"]
        labels = [label for _, label in self.ts_modes]
        nr_points = len(sched["points"][0])
        ttk.Label(tab, text="Tick a switch point to use it; times are HH:MM.",
                  foreground="#666").grid(row=0, column=0, columnspan=nr_points + 1, sticky="w", pady=(0, 6))
        for p in range(nr_points):
            ttk.Label(tab, text=f"Point {p + 1}").grid(row=1, column=p + 1, sticky="w")
        self.ts_cells = []
        for d, day in enumerate(sched["points"]):
            ttk.Label(tab, text=sched["days"][d]).grid(row=d + 2, column=0, sticky="w", padx=(0, 8), pady=2)
            cells = []
            for p, (t, mode, on) in enumerate(day):
                cell = ttk.Frame(tab)
                cell.grid(row=d + 2, column=p + 1, padx=(0, 6))
                on_var = tk.BooleanVar(value=on)
                t_var = tk.StringVar(value=t)
                m_var = tk.StringVar(value=dict(self.ts_modes).get(mode, labels[0]))
                chk = ttk.Checkbutton(cell, variable=on_var)
                ent = ttk.Entry(cell, textvariable=t_var, width=6)
                cmb = ttk.Combobox(cell, textvariable=m_var, values=labels, width=10, state="readonly")
                chk.pack(side="left")
                ent.pack(side="left")
                cmb.pack(side="left", padx=(2, 0))
                chk.config(command=lambda e=ent, c=cmb, v=on_var: self._toggle_point(e, c, v))
                self._toggle_point(ent, cmb, on_var)
                cells.append((t_var, m_var, on_var, ent, cmb))
            self.ts_cells.append(cells)

        row = len(sched["points"]) + 2
        copy = ttk.Frame(tab)
        copy.grid(row=row, column=0, columnspan=nr_points + 1, sticky="w", pady=(10, 0))
        ttk.Label(copy, text="Copy").pack(side="left")
        self.ts_from = tk.StringVar(value=sched["days"][0])
        ttk.Combobox(copy, textvariable=self.ts_from, values=sched["days"], width=11,
                     state="readonly").pack(side="left", padx=4)
        ttk.Label(copy, text="to").pack(side="left")
        self.ts_copy_to = []
        for name in sched["days"]:
            v = tk.BooleanVar()
            ttk.Checkbutton(copy, text=name[:3], variable=v).pack(side="left", padx=2)
            self.ts_copy_to.append(v)
        ttk.Button(copy, text="Copy", command=lambda: self._copy_day(sched["days"])).pack(side="left", padx=6)

        bar = ttk.Frame(tab)
        bar.grid(row=row + 1, column=0, columnspan=nr_points + 1, sticky="ew", pady=(10, 0))
        self.ts_status = ttk.Label(bar, text="Changes are only sent when you press Save.", foreground="#666")
        self.ts_status.pack(side="left")
        ttk.Button(bar, text="Save", command=self.save_schedule).pack(side="right")
        ttk.Button(bar, text="Reload", command=self.load_schedule).pack(side="right", padx=6)

    @staticmethod
    def _toggle_point(ent, cmb, on_var):
        ent.config(state="normal" if on_var.get() else "disabled")
        cmb.config(state="readonly" if on_var.get() else "disabled")

    def _copy_day(self, days):
        src = self.ts_cells[days.index(self.ts_from.get())]
        for d, v in enumerate(self.ts_copy_to):
            if not v.get() or self.ts_cells[d] is src:
                continue
            for (t, m, on, ent, cmb), (st, sm, son, _, _) in zip(self.ts_cells[d], src):
                t.set(st.get())
                m.set(sm.get())
                on.set(son.get())
                self._toggle_point(ent, cmb, on)
            v.set(False)
        self.ts_status.config(text="Copied – press Save to send.", foreground="#666")

    def save_schedule(self):
        by_label = {label: value for value, label in self.ts_modes}
        points = [[(t.get(), by_label[m.get()], on.get()) for t, m, on, _, _ in day] for day in self.ts_cells]
        try:
            for day in points:
                normalize_day(day)
        except ValueError as e:
            self.ts_status.config(text=str(e), foreground=WARN)
            return
        self.ts_status.config(text="Saving…", foreground="#666")
        gw, dp = self.gw, self.ts_dp

        def done(_, err):
            if not self._alive():
                return
            if err:
                self.ts_status.config(text=f"Save failed: {err}", foreground=WARN)
                return
            self.load_schedule()

        self.w.run(lambda: gw.write_schedule(dp, points), done)

    # -- Room state tab --

    def load_state(self):
        gw, page = self.gw, self.pages["Room state"]
        self.w.run(lambda: [(name, gw.read_text(dp)) for name, dp, _ in gw.datapoints(page)], self._show_state)

    def _show_state(self, rows, err):
        if not self._alive():
            return
        tab = self.st_tab
        self._clear(tab)
        if err:
            ttk.Label(tab, text=f"Could not load: {err}").grid(row=0, column=0)
        else:
            for r, (name, (value, unit)) in enumerate(rows):
                ttk.Label(tab, text=name).grid(row=r, column=0, sticky="w", pady=2)
                ttk.Label(tab, text=f"{value} {unit}".strip(), font=("Segoe UI Semibold", 9)).grid(
                    row=r, column=1, sticky="e", padx=(16, 0))
            ttk.Label(tab, text="updated " + time.strftime("%H:%M:%S"), foreground="#666").grid(
                row=len(rows), column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.after(self.STATE_REFRESH_MS, self.load_state)


if __name__ == "__main__":
    if check_for_update(load_config()):
        restart()
    root = tk.Tk()
    Widget(root)
    root.mainloop()
