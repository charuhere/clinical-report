#!/usr/bin/env python3
"""
Distributed Clinical Report Editing System
node.py — Full Implementation: P2P Sync + Web UI + Heartbeat + Recovery

One codebase, three different behaviors — driven entirely by config.json.

Run from inside a node folder:
    cd icu/ && python node.py
    cd rad/ && python node.py
"""

import socket
import threading
import json
import time
import sys
import shlex
import queue
import os
from datetime import datetime

# Flask for Web UI
from flask import Flask, Response, request, jsonify


# ═══════════════════════════════════════════════════════════════
#  LAMPORT CLOCK
#  - tick()     : local event (before sending)
#  - update(ts) : on receiving (max(local, received) + 1)
#  - value()    : read current clock
# ═══════════════════════════════════════════════════════════════
class LamportClock:
    def __init__(self):
        self._time = 0
        self._lock = threading.Lock()

    def tick(self):
        with self._lock:
            self._time += 1
            return self._time

    def update(self, received_ts):
        with self._lock:
            self._time = max(self._time, received_ts) + 1
            return self._time

    def value(self):
        with self._lock:
            return self._time


# ═══════════════════════════════════════════════════════════════
#  FILE I/O
# ═══════════════════════════════════════════════════════════════
def load_config():
    with open("config.json", encoding="utf-8") as f:
        return json.load(f)

def load_report():
    with open("report.json", encoding="utf-8") as f:
        return json.load(f)

def save_report(report):
    with open("report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

def write_audit(node_id, ts, operation, field, value):
    wall_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{wall_time}] [{node_id} | TS={ts}] {operation} {field} = \"{value}\"\n"
    with open("audit.log", "a", encoding="utf-8") as f:
        f.write(line)


# ═══════════════════════════════════════════════════════════════
#  REPORT FIELD HELPERS  (dot-notation access)
# ═══════════════════════════════════════════════════════════════
def get_field(report, path):
    obj = report
    for key in path.split("."):
        if not isinstance(obj, dict) or key not in obj:
            return None
        obj = obj[key]
    return obj

def set_field(report, path, value):
    keys = path.split(".")
    obj = report
    for key in keys[:-1]:
        obj = obj[key]
    obj[keys[-1]] = value

def apply_edit(report, operation, field, value):
    if operation == "update":
        set_field(report, field, value)
    elif operation == "append":
        current = get_field(report, field) or ""
        sep = " " if current.strip() else ""
        set_field(report, field, current + sep + value)


# ═══════════════════════════════════════════════════════════════
#  SSE CLIENTS — Server-Sent Events for Real-Time Browser Push
# ═══════════════════════════════════════════════════════════════
_sse_clients = []
_sse_lock    = threading.Lock()

def sse_broadcast(event_type, data):
    """Push an SSE event to all connected browser clients."""
    msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    dead = []
    with _sse_lock:
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)


# ═══════════════════════════════════════════════════════════════
#  DOCUMENT LOCK — Distributed Lock via Dashboard
# ═══════════════════════════════════════════════════════════════
_doc_locked    = False
_doc_lock_mutex = threading.Lock()

def is_doc_locked():
    with _doc_lock_mutex:
        return _doc_locked

def set_doc_locked(val):
    global _doc_locked
    with _doc_lock_mutex:
        _doc_locked = val


# ═══════════════════════════════════════════════════════════════
#  PEER STATUS — Heartbeat Tracking
# ═══════════════════════════════════════════════════════════════
peer_status = {}       # {"RAD": "ONLINE", ...}
last_seen   = {}       # {"RAD": <timestamp>, ...}
_peer_lock  = threading.Lock()

HEARTBEAT_INTERVAL = 3   # seconds between heartbeats
FAILURE_TIMEOUT    = 9   # seconds before marking peer OFFLINE


# ═══════════════════════════════════════════════════════════════
#  IN-MEMORY AUDIT LOG  (for dashboard & web UI)
# ═══════════════════════════════════════════════════════════════
_audit_entries = []
_audit_lock    = threading.Lock()

def add_audit_entry(node_id, ts, operation, field, value):
    entry = {
        "node_id":    node_id,
        "lamport_ts": ts,
        "operation":  operation,
        "field":      field,
        "value":      value,
        "time":       datetime.now().strftime("%H:%M:%S")
    }
    with _audit_lock:
        _audit_entries.append(entry)
    return entry


# ═══════════════════════════════════════════════════════════════
#  EDIT QUEUE — Heart of Conflict Resolution
#
#  All incoming edits (from peers) land here.
#  A background thread drains the queue every 150ms,
#  sorts edits by (lamport_ts, node_id) to get a TOTAL ORDER,
#  then applies them in that order.
# ═══════════════════════════════════════════════════════════════
_edit_queue      = []
_edit_queue_lock = threading.Lock()

def enqueue_edit(packet):
    with _edit_queue_lock:
        _edit_queue.append(packet)

def edit_processor(config, report, report_lock, clock):
    node_id = config["node_id"]

    while True:
        time.sleep(0.15)

        with _edit_queue_lock:
            if not _edit_queue:
                continue
            batch = sorted(_edit_queue, key=lambda e: (e["lamport_ts"], e["node_id"]))
            _edit_queue.clear()

        # Apply sorted batch
        for edit in batch:
            with report_lock:
                apply_edit(report, edit["operation"], edit["field"], edit["value"])
                save_report(report)
            write_audit(edit["node_id"], edit["lamport_ts"],
                        edit["operation"], edit["field"], edit["value"])
            add_audit_entry(edit["node_id"], edit["lamport_ts"],
                            edit["operation"], edit["field"], edit["value"])

        # Get report snapshot for notifications
        with report_lock:
            report_copy = json.loads(json.dumps(report))

        # ── SSE + Terminal notifications ──
        W = 56
        if len(batch) == 1:
            e = batch[0]
            sse_broadcast("report_update", {
                "report":   report_copy,
                "editor":   e["node_id"],
                "field":    e["field"],
                "value":    e["value"],
                "ts":       e["lamport_ts"],
                "conflict": False
            })
            print(f"\n{'─'*W}")
            print(f"  📋 REPORT UPDATED  [from {e['node_id']} | TS={e['lamport_ts']}]")
            print(f"     {e['field']}  →  \"{e['value']}\"")
            print(f"{'─'*W}")
        else:
            sse_broadcast("report_update", {
                "report":     report_copy,
                "editor":     batch[-1]["node_id"],
                "field":      batch[-1]["field"],
                "value":      batch[-1]["value"],
                "ts":         batch[-1]["lamport_ts"],
                "conflict":   True,
                "batch_size": len(batch)
            })
            print(f"\n{'═'*W}")
            print(f"  ⚡ CONFLICT DETECTED & RESOLVED  ({len(batch)} simultaneous edits)")
            print(f"{'─'*W}")
            for i, e in enumerate(batch):
                tag = " ← applied FIRST (wins)" if i == 0 else " ← applied after"
                print(f"  [{e['node_id']} | TS={e['lamport_ts']}]  {e['field']} → \"{e['value']}\"{tag}")
            print(f"{'═'*W}")

        # Notify admin dashboard
        notify_dashboard(config, report_copy, batch[-1])
        print(f"[{node_id} | TS={clock.value()}]> ", end="", flush=True)


# ═══════════════════════════════════════════════════════════════
#  DASHBOARD NOTIFICATION — UDP push to admin dashboard
# ═══════════════════════════════════════════════════════════════
def notify_dashboard(config, report, last_edit):
    try:
        with _peer_lock:
            status = dict(peer_status)
        status[config["node_id"]] = "ONLINE"

        with _audit_lock:
            audit_copy = list(_audit_entries[-50:])   # last 50 entries

        packet = {
            "type":         "DASHBOARD_UPDATE",
            "report":       report,
            "node_id":      last_edit.get("node_id", config["node_id"]),
            "field":        last_edit.get("field", ""),
            "value":        last_edit.get("value", ""),
            "lamport_ts":   last_edit.get("lamport_ts", 0),
            "operation":    last_edit.get("operation", ""),
            "nodes_status": status,
            "audit_log":    audit_copy
        }

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        data = json.dumps(packet).encode()
        sock.sendto(data, (config.get("dashboard_host", "localhost"), config.get("dashboard_port", 7000)))
        sock.close()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  TCP SERVER — receives edits, snapshots, lock commands
# ═══════════════════════════════════════════════════════════════
def handle_connection(conn, config, clock, report, report_lock):
    try:
        data = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        if data:
            packet = json.loads(data.decode())
            dispatch_packet(packet, config, clock, report, report_lock)
    except Exception:
        pass
    finally:
        conn.close()

def dispatch_packet(packet, config, clock, report, report_lock):
    ptype = packet.get("type")

    if ptype == "EDIT":
        if is_doc_locked():
            return
        clock.update(packet["lamport_ts"])
        enqueue_edit(packet)

    elif ptype == "REJOIN_REQUEST":
        requester_id = packet["node_id"]
        with report_lock:
            snapshot = json.loads(json.dumps(report))
        with _audit_lock:
            audit_copy = list(_audit_entries)

        response = {
            "type":       "SNAPSHOT",
            "report":     snapshot,
            "lamport_ts": clock.value(),
            "node_id":    config["node_id"],
            "audit_log":  audit_copy
        }
        for peer in config["peers"]:
            if peer["node_id"] == requester_id:
                tcp_send(peer["host"], peer["tcp_port"], response)
                break

    elif ptype == "SNAPSHOT":
        received_ts = packet["lamport_ts"]
        with report_lock:
            if received_ts > clock.value():
                for key, val in packet["report"].items():
                    report[key] = val
                save_report(report)
                clock.update(received_ts)

                if "audit_log" in packet and packet["audit_log"]:
                    with _audit_lock:
                        if len(packet["audit_log"]) > len(_audit_entries):
                            _audit_entries.clear()
                            _audit_entries.extend(packet["audit_log"])

                report_copy = json.loads(json.dumps(report))
                sse_broadcast("state_recovered", {
                    "report":    report_copy,
                    "from_node": packet["node_id"],
                    "ts":        received_ts
                })
                print(f"\n  ✅ State recovered from {packet['node_id']} (TS={received_ts})")
                print(f"[{config['node_id']} | TS={clock.value()}]> ", end="", flush=True)

    elif ptype == "LOCK":
        set_doc_locked(True)
        sse_broadcast("lock_status", {"locked": True})
        print(f"\n  🔒 Document LOCKED by ADMIN")
        print(f"[{config['node_id']} | TS={clock.value()}]> ", end="", flush=True)

    elif ptype == "UNLOCK":
        set_doc_locked(False)
        sse_broadcast("lock_status", {"locked": False})
        print(f"\n  🔓 Document UNLOCKED by ADMIN")
        print(f"[{config['node_id']} | TS={clock.value()}]> ", end="", flush=True)

def tcp_server(config, clock, report, report_lock):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", config["tcp_port"]))
    sock.listen(10)
    while True:
        conn, _ = sock.accept()
        threading.Thread(
            target=handle_connection,
            args=(conn, config, clock, report, report_lock),
            daemon=True
        ).start()


# ═══════════════════════════════════════════════════════════════
#  TCP SEND / BROADCAST
# ═══════════════════════════════════════════════════════════════
def tcp_send(host, port, packet):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect((host, port))
        s.sendall(json.dumps(packet).encode())
        s.close()
        return True
    except Exception:
        return False

def broadcast(config, packet):
    results = {}
    for peer in config["peers"]:
        ok = tcp_send(peer["host"], peer["tcp_port"], packet)
        results[peer["node_id"]] = ok
        icon = "✓" if ok else "✗"
        label = "synced" if ok else "unreachable"
        print(f"  [→ {peer['node_id']}]  {icon} {label}")
    return results


# ═══════════════════════════════════════════════════════════════
#  UDP HEARTBEAT — Failure Detection
# ═══════════════════════════════════════════════════════════════
def heartbeat_sender(config):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    msg  = json.dumps({"type": "HEARTBEAT", "node_id": config["node_id"]}).encode()
    while True:
        for peer in config["peers"]:
            try:
                sock.sendto(msg, (peer["host"], peer["udp_port"]))
            except Exception:
                pass
        time.sleep(HEARTBEAT_INTERVAL)

def heartbeat_listener(config):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", config["udp_port"]))
    while True:
        try:
            data, _ = sock.recvfrom(1024)
            pkt = json.loads(data.decode())
            if pkt.get("type") == "HEARTBEAT":
                pid = pkt["node_id"]
                with _peer_lock:
                    was_offline = peer_status.get(pid) == "OFFLINE"
                    last_seen[pid] = time.time()
                    peer_status[pid] = "ONLINE"
                if was_offline:
                    print(f"\n  ✅ {pid} is back ONLINE")
                    sse_broadcast("peer_status", {"peer_id": pid, "status": "ONLINE"})
        except Exception:
            pass

def failure_detector(config, clock):
    node_id = config["node_id"]
    while True:
        time.sleep(HEARTBEAT_INTERVAL)
        with _peer_lock:
            for peer in config["peers"]:
                pid = peer["node_id"]
                if pid in last_seen:
                    elapsed = time.time() - last_seen[pid]
                    if elapsed > FAILURE_TIMEOUT and peer_status.get(pid) != "OFFLINE":
                        peer_status[pid] = "OFFLINE"
                        print(f"\n  ⚠️  {pid} is OFFLINE (no heartbeat for {int(elapsed)}s)")
                        print(f"[{node_id} | TS={clock.value()}]> ", end="", flush=True)
                        sse_broadcast("peer_status", {"peer_id": pid, "status": "OFFLINE"})


# ═══════════════════════════════════════════════════════════════
#  LOCAL EDIT HANDLER
#  Called by terminal AND Flask POST endpoint.
#  Returns (bool, message) for Flask; prints for terminal.
# ═══════════════════════════════════════════════════════════════
def do_edit(config, clock, report, report_lock, operation, field, value):
    node_id = config["node_id"]
    owned   = config["owned_fields"]

    # ── Document lock check ──
    if is_doc_locked():
        msg = "🔒 Document is LOCKED. No edits allowed."
        print(f"\n  ❌  {msg}")
        return False, msg

    # ── Decentralized Access Control ──
    if field not in owned:
        msg = f"ACCESS DENIED — '{field}' is not owned by {node_id}."
        print(f"\n  ❌  {msg}")
        print(f"       Your fields: {', '.join(owned)}\n")
        return False, msg

    ts = clock.tick()
    packet = {
        "type":       "EDIT",
        "node_id":    node_id,
        "lamport_ts": ts,
        "operation":  operation,
        "field":      field,
        "value":      value
    }

    # Apply locally
    with report_lock:
        apply_edit(report, operation, field, value)
        save_report(report)

    write_audit(node_id, ts, operation, field, value)
    add_audit_entry(node_id, ts, operation, field, value)
    print(f"\n  ✅  [{node_id} | TS={ts}]  {operation}  {field}  →  \"{value}\"")

    # Broadcast to peers
    broadcast(config, packet)

    # Notify browser (SSE)
    with report_lock:
        report_copy = json.loads(json.dumps(report))
    sse_broadcast("report_update", {
        "report":   report_copy,
        "editor":   node_id,
        "field":    field,
        "value":    value,
        "ts":       ts,
        "conflict": False
    })

    # Notify dashboard
    notify_dashboard(config, report_copy, packet)

    print()
    return True, f"[{node_id} | TS={ts}] {operation} {field} → \"{value}\""


# ═══════════════════════════════════════════════════════════════
#  CONFLICT TEST
# ═══════════════════════════════════════════════════════════════
def run_conflict_test(config, clock, report, report_lock):
    node_id = config["node_id"]
    peer    = config["peers"][0]
    field   = config["owned_fields"][0] if config["owned_fields"] else "icu.bp"
    W       = 56

    ts = clock.tick()

    edit_mine = {
        "type": "EDIT", "node_id": node_id, "lamport_ts": ts,
        "operation": "update", "field": field,
        "value": f"Entry by {node_id} at TS={ts}"
    }
    edit_peer = {
        "type": "EDIT", "node_id": peer["node_id"], "lamport_ts": ts,
        "operation": "update", "field": field,
        "value": f"Entry by {peer['node_id']} at TS={ts}"
    }

    print(f"\n{'═'*W}")
    print(f"  ⚡ CONFLICT TEST — LAMPORT CLOCK RESOLUTION")
    print(f"{'═'*W}")
    print(f"  Field under conflict: {field}")
    print(f"  Scenario: {node_id} and {peer['node_id']} both write")
    print(f"            to the same field at TS={ts} simultaneously.")
    print(f"{'─'*W}")
    print(f"  Edit A:  [{node_id:<4} | TS={ts}]  →  \"{edit_mine['value']}\"")
    print(f"  Edit B:  [{peer['node_id']:<4} | TS={ts}]  →  \"{edit_peer['value']}\"")
    print(f"{'─'*W}")
    print(f"  RESOLUTION RULE:")
    print(f"    Step 1 — Compare Lamport timestamps: {ts} == {ts}  → TIE")
    print(f"    Step 2 — Tiebreak by node_id (alphabetical order)")

    ordered = sorted([edit_mine, edit_peer],
                     key=lambda e: (e["lamport_ts"], e["node_id"]))
    winner, loser = ordered[0], ordered[1]

    print(f"{'─'*W}")
    print(f"  ✅ WINNER : [{winner['node_id']:<4} | TS={ts}]  ('{winner['node_id']}' < '{loser['node_id']}')")
    print(f"  📝 ORDER  : {winner['node_id']} applied first, {loser['node_id']} applied second")
    print(f"{'─'*W}")

    with report_lock:
        apply_edit(report, winner["operation"], winner["field"], winner["value"])
        apply_edit(report, loser["operation"],  loser["field"],  loser["value"])
        save_report(report)

    write_audit(node_id, ts, "CONFLICT-TEST", field, f"winner={winner['node_id']}")

    final_val = get_field(report, field)
    print(f"  Final value of {field}:")
    print(f"  \"{final_val}\"")
    print(f"  (Last write wins after total ordering — all nodes converge here.)")
    print(f"{'═'*W}\n")

    # Notify browser
    with report_lock:
        report_copy = json.loads(json.dumps(report))
    sse_broadcast("conflict_test", {
        "report": report_copy, "field": field,
        "winner": winner["node_id"], "loser": loser["node_id"], "ts": ts
    })


# ═══════════════════════════════════════════════════════════════
#  REJOIN — State Recovery
# ═══════════════════════════════════════════════════════════════
def request_state_recovery(config):
    node_id = config["node_id"]
    packet  = {"type": "REJOIN_REQUEST", "node_id": node_id}
    print(f"  📡 Requesting state recovery from peers...")
    for peer in config["peers"]:
        ok = tcp_send(peer["host"], peer["tcp_port"], packet)
        if ok:
            print(f"    → Sent REJOIN_REQUEST to {peer['node_id']}")
        else:
            print(f"    → {peer['node_id']} unreachable")


# ═══════════════════════════════════════════════════════════════
#  DISPLAY HELPERS  (Terminal)
# ═══════════════════════════════════════════════════════════════
W = 56

def print_banner(config):
    icons = {"ICU": "🏥", "RAD": "🔬"}
    role_icon = "👨‍⚕️" if config["role"] == "doctor" else "🩺"
    node_icon = icons.get(config["node_id"], "📋")
    print("\n" + "═"*W)
    print(f"   {node_icon}  DISTRIBUTED CLINICAL REPORT SYSTEM")
    print(f"   {role_icon}  Node: {config['node_id']}   Role: {config['role'].upper()}")
    print("═"*W)
    print("   Commands:")
    print("     view                        — show full patient report")
    print("     update <field> <value>      — update a field")
    print("     append <field> <text>       — append text to a field")
    print("     status                      — clock, role, peers")
    print("     conflict-test               — demo conflict resolution")
    print("     exit                        — shutdown this node")
    print("─"*W)
    print(f"   Your owned fields:")
    for f in config["owned_fields"]:
        print(f"     • {f}")
    print(f"\n   🌐 Web UI: http://localhost:{config['http_port']}")
    print("═"*W + "\n")

def print_report(report, node_id, ts):
    print("\n" + "═"*W)
    print(f"  PATIENT REPORT   [{node_id} | TS={ts}]")
    print("═"*W)
    print(f"  Patient   : {report['patient_name']}, {report['age']}M")
    print(f"  Admitted  : {report['admission_date']}")
    print(f"  Diagnosis : {report['diagnosis']}")
    print("─"*W)
    print("  ICU FINDINGS")
    print(f"    BP                : {report['icu']['bp']}")
    print(f"    Oxygen Saturation : {report['icu']['oxygen_saturation']}")
    print(f"    Heart Rate        : {report['icu']['heart_rate']}")
    print(f"    Treatment         : {report['icu']['treatment']}")
    print(f"    ICU Notes         : {report['icu']['notes']}")
    print("─"*W)
    print("  RADIOLOGY")
    print(f"    X-Ray             : {report['radiology']['xray']}")
    print(f"    Echo              : {report['radiology']['echo']}")
    print(f"    Radiology Notes   : {report['radiology']['notes']}")
    print("═"*W + "\n")

def print_status(config, clock):
    print("\n" + "─"*W)
    print(f"  Node ID       : {config['node_id']}")
    print(f"  Role          : {config['role'].upper()}")
    print(f"  Lamport Clock : {clock.value()}")
    print(f"  TCP Port      : {config['tcp_port']}")
    print(f"  UDP Port      : {config['udp_port']}")
    print(f"  HTTP Port     : {config['http_port']}")
    print(f"  Owned Fields  : {', '.join(config['owned_fields'])}")
    print("  Peers:")
    with _peer_lock:
        for p in config["peers"]:
            status = peer_status.get(p['node_id'], 'UNKNOWN')
            icon = "✅" if status == "ONLINE" else "❌"
            print(f"    • {p['node_id']}  {p['host']}:{p['tcp_port']}  {icon} {status}")
    print("─"*W + "\n")


# ═══════════════════════════════════════════════════════════════
#  FLASK WEB UI
# ═══════════════════════════════════════════════════════════════
app = Flask(__name__)

# Globals set in main()
_config      = None
_clock       = None
_report      = None
_report_lock = None

@app.route("/")
def serve_ui():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    try:
        with open(html_path, encoding="utf-8") as f:
            return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}
    except FileNotFoundError:
        return "index.html not found in node directory", 404

@app.route("/api/config")
def api_config():
    return jsonify({
        "node_id":      _config["node_id"],
        "role":         _config["role"],
        "owned_fields": _config["owned_fields"],
        "peers":        [{"node_id": p["node_id"]} for p in _config["peers"]]
    })

@app.route("/api/report")
def api_report():
    with _report_lock:
        return jsonify({
            "report":     json.loads(json.dumps(_report)),
            "lamport_ts": _clock.value(),
            "locked":     is_doc_locked()
        })

@app.route("/api/status")
def api_status():
    with _peer_lock:
        st = dict(peer_status)
    return jsonify({
        "node_id":    _config["node_id"],
        "role":       _config["role"],
        "lamport_ts": _clock.value(),
        "peers":      st,
        "locked":     is_doc_locked()
    })

@app.route("/api/edit", methods=["POST"])
def api_edit():
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "error": "No JSON body"}), 400

    operation = data.get("operation", "update")
    field     = data.get("field")
    value     = data.get("value")

    if not field or value is None:
        return jsonify({"ok": False, "error": "Missing field or value"}), 400

    ok, msg = do_edit(_config, _clock, _report, _report_lock, operation, field, value)
    if ok:
        return jsonify({"ok": True, "message": msg, "lamport_ts": _clock.value()})
    else:
        return jsonify({"ok": False, "error": msg}), 403

@app.route("/api/events")
def api_events():
    """SSE endpoint — browser connects once, receives all updates."""
    q = queue.Queue()
    with _sse_lock:
        _sse_clients.append(q)

    def stream():
        try:
            # Send initial state immediately
            with _report_lock:
                rpt = json.loads(json.dumps(_report))
            with _peer_lock:
                st = dict(peer_status)
            initial = {
                "report": rpt, "ts": _clock.value(),
                "locked": is_doc_locked(), "peers": st
            }
            yield f"event: initial_state\ndata: {json.dumps(initial)}\n\n"

            while True:
                try:
                    msg = q.get(timeout=25)
                    yield msg
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            with _sse_lock:
                if q in _sse_clients:
                    _sse_clients.remove(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})

@app.route("/api/audit")
def api_audit():
    with _audit_lock:
        return jsonify(list(_audit_entries))


# ═══════════════════════════════════════════════════════════════
#  TERMINAL INPUT LOOP
# ═══════════════════════════════════════════════════════════════
def terminal_loop(config, clock, report, report_lock):
    node_id = config["node_id"]

    while True:
        try:
            ts  = clock.value()
            cmd = input(f"[{node_id} | TS={ts}]> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n  [{node_id}] Shutting down gracefully...")
            sys.exit(0)

        if not cmd:
            continue

        try:
            parts = shlex.split(cmd)
        except ValueError as e:
            print(f"  Parse error: {e}  — wrap multi-word values in quotes.")
            continue

        command = parts[0].lower()

        if command == "view":
            with report_lock:
                print_report(report, node_id, clock.value())

        elif command == "update":
            if len(parts) < 3:
                print("  Usage:   update <field> <value>")
                print("  Example: update icu.bp \"120/80 mmHg\"")
                continue
            do_edit(config, clock, report, report_lock, "update", parts[1], " ".join(parts[2:]))

        elif command == "append":
            if len(parts) < 3:
                print("  Usage:   append <field> <text>")
                print("  Example: append icu.notes \"Patient stable.\"")
                continue
            do_edit(config, clock, report, report_lock, "append", parts[1], " ".join(parts[2:]))

        elif command == "status":
            print_status(config, clock)

        elif command == "conflict-test":
            run_conflict_test(config, clock, report, report_lock)

        elif command == "exit":
            print(f"\n  [{node_id}] Shutting down gracefully...")
            sys.exit(0)

        else:
            print(f"  Unknown command: '{command}'")
            print("  Commands: view | update | append | status | conflict-test | exit")


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════
def main():
    global _config, _clock, _report, _report_lock

    config      = load_config()
    report      = load_report()
    report_lock = threading.Lock()
    clock       = LamportClock()

    _config      = config
    _clock       = clock
    _report      = report
    _report_lock = report_lock

    # Initialize peer status
    with _peer_lock:
        for peer in config["peers"]:
            peer_status[peer["node_id"]] = "UNKNOWN"
            last_seen[peer["node_id"]]   = time.time()

    print_banner(config)

    # ── Thread 1: TCP server (receives edits from peers) ──
    threading.Thread(target=tcp_server,
                     args=(config, clock, report, report_lock),
                     daemon=True).start()

    # ── Thread 2: Edit processor (conflict resolution) ──
    threading.Thread(target=edit_processor,
                     args=(config, report, report_lock, clock),
                     daemon=True).start()

    # ── Thread 3: UDP heartbeat sender ──
    threading.Thread(target=heartbeat_sender,
                     args=(config,), daemon=True).start()

    # ── Thread 4: UDP heartbeat listener ──
    threading.Thread(target=heartbeat_listener,
                     args=(config,), daemon=True).start()

    # ── Thread 5: Failure detector ──
    threading.Thread(target=failure_detector,
                     args=(config, clock), daemon=True).start()

    # ── Thread 6: Flask web server ──
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=config["http_port"], threaded=True),
        daemon=True
    ).start()

    print(f"  ✅  [{config['node_id']}] Node is ONLINE")
    print(f"      TCP: {config['tcp_port']}  |  UDP: {config['udp_port']}  |  HTTP: {config['http_port']}")
    print(f"  🌐  Open http://localhost:{config['http_port']} in browser")
    print(f"  ⏳  Waiting for peers...\n")

    # Request state from peers (recovery on restart)
    time.sleep(1)
    request_state_recovery(config)

    # ── Main thread: terminal input loop ──
    terminal_loop(config, clock, report, report_lock)


if __name__ == "__main__":
    main()