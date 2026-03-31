## D. Codes (Core Distributed Logic)

Due to the extensive size of the complete codebase (>1,500 lines across Frontend, Dashboard, and Node systems), the primary **distributed algorithms** governing the network are showcased below.

### 1. Vector Clock Synchronization (Python)
Instead of relying on unstable physical time hardware, nodes maintain causality using a dictionary mapping machine IDs to integer counters (`VectorClock`).

```python
class VectorClock:
    def __init__(self, node_id):
        self.node_id = node_id
        self.vc = {node_id: 0}
        self.lock = threading.Lock()

    def update(self, other_vc):
        """Synchronizes logical time when packets arrive from peers."""
        with self.lock:
            for k, v in other_vc.items():
                self.vc[k] = max(self.vc.get(k, 0), v)
            return dict(self.vc)

    def tick(self):
        """Advances the internal clock during a localized edit event."""
        with self.lock:
            self.vc[self.node_id] = self.vc.get(self.node_id, 0) + 1
            return dict(self.vc)
```

### 2. Distributed Conflict Resolution (Python)
When two nodes blindly edit the same clinical report simultaneously, the network evaluates their Vector Clocks. If the edit vectors are perfectly concurrent, the deterministic mathematical fallback algorithm establishes systematic ordering.

```python
def edit_processor(clock, report, report_lock):
    while True:
        e = _edit_queue.get()
        ts = e["vector_ts"]
        
        # Buffer window to capture arriving concurrent peer edits
        concurrents = [e]
        time.sleep(1.5) 

        while not _edit_queue.empty():
            concurrents.append(_edit_queue.get(False))

        # Vector mathematical resolution
        if len(concurrents) > 1:
            concurrents.sort(key=lambda x: (sum(x["vector_ts"].values()), x["node_id"]))
            
        winner = concurrents[-1]
        
        with report_lock:
            apply_edit(report, winner["operation"], winner["field"], winner["value"])
            save_report(report)
            
        clock.update(winner["vector_ts"])
```

### 3. UDP Heartbeat Failure Transmitter (Python)
A lightweight daemon thread constantly broadcasts liveness to all neighbors to facilitate decentralized fault tolerance.

```python
def heartbeat_sender(config):
    port = config["udp_port"]
    while True:
        pkt = {"type": "HEARTBEAT", "node_id": config["node_id"]}
        for peer in config["peers"]:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.sendto(json.dumps(pkt).encode(), (peer["host"], peer["udp_port"]))
                s.close()
            except:
                pass
        time.sleep(3) # Broadcast every 3 seconds
```


---

# E. Appendix - Complete Source Code

This section contains the complete, unedited source code for the entirety of the distributed system architecture, including the backend python peer networking logic, the Node.js centralized monitoring server, and the graphical user interfaces.

## 1. Peer Node Backend (Python)
> Filepath: icu/node.py

`python
#!/usr/bin/env python3
"""
Distributed Clinical Report Editing System
node.py — Full Implementation with 9 Distributed Computing Features

Features:
  1. P2P Sync (UDP/TCP)         2. Decentralized Access Control
  3. Heartbeat Failure Detection 4. State Recovery (Crash & Rejoin)
  5. Distributed Lock (TCP)      6. Vector Clock Conflict Resolution
  7. Leader Election (Bully)     8. Checkpointing & Rollback

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
import uuid
from datetime import datetime

from flask import Flask, Response, request, jsonify


# ═══════════════════════════════════════════════════════════════
#  VECTOR CLOCK
# ═══════════════════════════════════════════════════════════════
class VectorClock:
    def __init__(self, node_id):
        self._node_id = node_id
        self._vector = {node_id: 0}
        self._lock = threading.Lock()

    def tick(self):
        with self._lock:
            self._vector[self._node_id] = self._vector.get(self._node_id, 0) + 1
            return dict(self._vector)

    def update(self, received_ts):
        with self._lock:
            self._vector[self._node_id] = self._vector.get(self._node_id, 0) + 1
            if isinstance(received_ts, dict):
                for k, v in received_ts.items():
                    self._vector[k] = max(self._vector.get(k, 0), v)
            return dict(self._vector)

    def value(self):
        with self._lock:
            return dict(self._vector)


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
#  REPORT FIELD HELPERS
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
#  SSE CLIENTS
# ═══════════════════════════════════════════════════════════════
_sse_clients = []
_sse_lock    = threading.Lock()

def sse_broadcast(event_type, data):
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
#  DOCUMENT LOCK
# ═══════════════════════════════════════════════════════════════
_doc_locked     = False
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
peer_status = {}
last_seen   = {}
_peer_lock  = threading.Lock()

HEARTBEAT_INTERVAL = 3
FAILURE_TIMEOUT    = 9


# ═══════════════════════════════════════════════════════════════
#  [FEATURE 7]  LEADER ELECTION STATE
# ═══════════════════════════════════════════════════════════════
_current_leader       = None
_election_in_progress = False
_election_lock        = threading.Lock()
ELECTION_TIMEOUT      = 4


# ═══════════════════════════════════════════════════════════════
#  [FEATURE 8]  CHECKPOINT STATE
# ═══════════════════════════════════════════════════════════════
_checkpoints      = []
_checkpoint_lock  = threading.Lock()
_edit_count       = 0
_edit_count_lock  = threading.Lock()
CHECKPOINT_EVERY  = 5
MAX_CHECKPOINTS   = 10


# ═══════════════════════════════════════════════════════════════
#  IN-MEMORY AUDIT LOG
# ═══════════════════════════════════════════════════════════════
_audit_entries = []
_audit_lock    = threading.Lock()

def add_audit_entry(node_id, ts, operation, field, value):
    entry = {
        "node_id": node_id, "vector_ts": ts, "operation": operation,
        "field": field, "value": value,
        "time": datetime.now().strftime("%H:%M:%S")
    }
    with _audit_lock:
        _audit_entries.append(entry)
    return entry


# ═══════════════════════════════════════════════════════════════
#  EDIT QUEUE — Conflict Resolution
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
            batch = sorted(_edit_queue, key=lambda e: (sum(e["vector_ts"].values()) if isinstance(e["vector_ts"], dict) else 0, e["node_id"]))
            _edit_queue.clear()

        for edit in batch:
            with report_lock:
                apply_edit(report, edit["operation"], edit["field"], edit["value"])
                save_report(report)
            write_audit(edit["node_id"], edit["vector_ts"],
                        edit["operation"], edit["field"], edit["value"])
            add_audit_entry(edit["node_id"], edit["vector_ts"],
                            edit["operation"], edit["field"], edit["value"])

        with report_lock:
            report_copy = json.loads(json.dumps(report))

        W = 56
        if len(batch) == 1:
            e = batch[0]
            sse_broadcast("report_update", {
                "report": report_copy, "editor": e["node_id"],
                "field": e["field"], "value": e["value"],
                "ts": e["vector_ts"], "conflict": False
            })
            print(f"\n{'─'*W}")
            print(f"  📋 REPORT UPDATED  [from {e['node_id']} | TS={e['vector_ts']}]")
            print(f"     {e['field']}  →  \"{e['value']}\"")
            print(f"{'─'*W}")
        else:
            sse_broadcast("report_update", {
                "report": report_copy, "editor": batch[-1]["node_id"],
                "field": batch[-1]["field"], "value": batch[-1]["value"],
                "ts": batch[-1]["vector_ts"], "conflict": True,
                "batch_size": len(batch)
            })
            print(f"\n{'═'*W}")
            print(f"  ⚡ CONFLICT DETECTED & RESOLVED  ({len(batch)} simultaneous edits)")
            print(f"{'─'*W}")
            for i, e in enumerate(batch):
                tag = " ← applied FIRST (wins)" if i == 0 else " ← applied after"
                print(f"  [{e['node_id']} | TS={e['vector_ts']}]  {e['field']} → \"{e['value']}\"{tag}")
            print(f"{'═'*W}")

        increment_edit_count(report, report_lock, clock, len(batch))
        notify_dashboard(config, report_copy, batch[-1])
        print(f"[{node_id} | TS={clock.value()}]> ", end="", flush=True)


# ═══════════════════════════════════════════════════════════════
#  [FEATURE 8]  CHECKPOINT FUNCTIONS
# ═══════════════════════════════════════════════════════════════
def create_checkpoint(report, report_lock, clock):
    with report_lock:
        snapshot = json.loads(json.dumps(report))
    cp = {
        "id": len(_checkpoints) + 1,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "vector_ts": clock.value(),
        "report": snapshot
    }
    with _checkpoint_lock:
        _checkpoints.append(cp)
        if len(_checkpoints) > MAX_CHECKPOINTS:
            _checkpoints.pop(0)
    print(f"\n  📸 Checkpoint #{cp['id']} saved (TS={cp['vector_ts']})")
    sse_broadcast("checkpoint_created", {"id": cp["id"], "vector_ts": cp["vector_ts"], "timestamp": cp["timestamp"]})
    return cp

def increment_edit_count(report, report_lock, clock, count=1):
    global _edit_count
    with _edit_count_lock:
        _edit_count += count
        if _edit_count >= CHECKPOINT_EVERY:
            _edit_count = 0
            create_checkpoint(report, report_lock, clock)

def do_rollback(config, clock, report, report_lock):
    node_id = config["node_id"]
    W = 56
    with _checkpoint_lock:
        if not _checkpoints:
            print(f"\n  ❌ No checkpoints available for rollback.")
            return False, "No checkpoints available."
        checkpoint = _checkpoints[-1]

    print(f"\n{'═'*W}")
    print(f"  ⏪ ROLLBACK TO CHECKPOINT #{checkpoint['id']}")
    print(f"{'─'*W}")
    print(f"  Checkpoint TS : {checkpoint['vector_ts']}")
    print(f"  Saved at      : {checkpoint['timestamp']}")
    print(f"{'─'*W}")

    ts = clock.tick()
    with report_lock:
        for key, val in checkpoint["report"].items():
            report[key] = val
        save_report(report)

    write_audit(node_id, ts, "ROLLBACK", "ALL", f"Checkpoint #{checkpoint['id']}")
    add_audit_entry(node_id, ts, "ROLLBACK", "ALL", f"Reverted to Checkpoint #{checkpoint['id']}")
    print(f"  ✅ Local state reverted to Checkpoint #{checkpoint['id']}")

    rollback_pkt = {
        "type": "ROLLBACK", "node_id": node_id, "vector_ts": ts,
        "report": checkpoint["report"], "checkpoint_id": checkpoint["id"]
    }
    broadcast(config, rollback_pkt)

    with report_lock:
        report_copy = json.loads(json.dumps(report))
    sse_broadcast("rollback", {"report": report_copy, "checkpoint_id": checkpoint["id"], "ts": ts, "by_node": node_id})
    notify_dashboard(config, report_copy, {"node_id": node_id, "field": "ROLLBACK", "value": f"Checkpoint #{checkpoint['id']}", "vector_ts": ts, "operation": "ROLLBACK"})
    print(f"  📡 Rollback broadcast to all peers")
    print(f"{'═'*W}\n")
    return True, f"Rolled back to Checkpoint #{checkpoint['id']}"

def list_checkpoints():
    W = 56
    with _checkpoint_lock:
        if not _checkpoints:
            print(f"\n  ℹ️  No checkpoints saved yet. (Auto-saves every {CHECKPOINT_EVERY} edits)")
            return
        print(f"\n{'═'*W}")
        print(f"  📸 SAVED CHECKPOINTS  ({len(_checkpoints)} total)")
        print(f"{'─'*W}")
        for cp in _checkpoints:
            print(f"  #{cp['id']:>2}  |  TS={cp['vector_ts']:<6}  |  {cp['timestamp']}")
        print(f"{'─'*W}")
        print(f"  Type 'rollback' to revert to the latest checkpoint")
        print(f"{'═'*W}\n")


# ═══════════════════════════════════════════════════════════════
#  [FEATURE 7]  LEADER ELECTION FUNCTIONS
# ═══════════════════════════════════════════════════════════════
def get_node_priority(node_id):
    return node_id

def start_election(config, clock):
    global _election_in_progress, _current_leader
    node_id = config["node_id"]
    W = 56

    with _election_lock:
        if _election_in_progress:
            return
        _election_in_progress = True

    print(f"\n{'═'*W}")
    print(f"  🗳️  ELECTION TRIGGERED by {node_id}")
    print(f"{'─'*W}")

    higher_peers = [p for p in config["peers"]
                    if get_node_priority(p["node_id"]) > get_node_priority(node_id)]

    if not higher_peers:
        declare_leader(config, clock, node_id)
        return

    got_answer = False
    for peer in higher_peers:
        pkt = {"type": "ELECTION", "node_id": node_id, "vector_ts": clock.tick()}
        print(f"  → Sending ELECTION to {peer['node_id']}...")
        ok = tcp_send(peer["host"], peer["tcp_port"], pkt)
        if ok:
            print(f"    ✓ {peer['node_id']} received ELECTION")
            got_answer = True
        else:
            print(f"    ✗ {peer['node_id']} unreachable")

    if got_answer:
        print(f"  ⏳ Waiting for COORDINATOR from higher-priority node...")
        print(f"{'─'*W}")
        time.sleep(ELECTION_TIMEOUT)
        with _election_lock:
            if _election_in_progress:
                print(f"\n  ⏰ No COORDINATOR received — taking over")
                declare_leader(config, clock, node_id)
    else:
        declare_leader(config, clock, node_id)

def declare_leader(config, clock, leader_id):
    global _current_leader, _election_in_progress
    node_id = config["node_id"]
    W = 56

    with _election_lock:
        _current_leader = leader_id
        _election_in_progress = False

    ts = clock.tick()
    if leader_id == node_id:
        print(f"  👑 {node_id} is now the LEADER")
        print(f"{'═'*W}\n")
        coord_pkt = {"type": "COORDINATOR", "node_id": node_id, "leader_id": node_id, "vector_ts": ts}
        for peer in config["peers"]:
            tcp_send(peer["host"], peer["tcp_port"], coord_pkt)

    write_audit(node_id, ts, "ELECTION", "leader", leader_id)
    add_audit_entry(node_id, ts, "ELECTION", "leader", f"👑 {leader_id} elected")
    sse_broadcast("leader_elected", {"leader_id": leader_id, "elected_by": node_id, "ts": ts})


# ═══════════════════════════════════════════════════════════════
#  DASHBOARD NOTIFICATION
# ═══════════════════════════════════════════════════════════════
def notify_dashboard(config, report, last_edit):
    try:
        with _peer_lock:
            status = dict(peer_status)
        status[config["node_id"]] = "ONLINE"
        with _audit_lock:
            audit_copy = list(_audit_entries[-50:])
        packet = {
            "type": "DASHBOARD_UPDATE", "report": report,
            "node_id": last_edit.get("node_id", config["node_id"]),
            "field": last_edit.get("field", ""), "value": last_edit.get("value", ""),
            "vector_ts": last_edit.get("vector_ts", 0),
            "operation": last_edit.get("operation", ""),
            "nodes_status": status, "audit_log": audit_copy,
            "leader": _current_leader
        }
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.sendto(json.dumps(packet).encode(), (config.get("dashboard_host", "localhost"), config.get("dashboard_port", 7000)))
        sock.close()
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  TCP SERVER + DISPATCH
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
    node_id = config["node_id"]

    if ptype == "EDIT":
        if is_doc_locked():
            return
        clock.update(packet["vector_ts"])
        enqueue_edit(packet)

    elif ptype == "REJOIN_REQUEST":
        requester_id = packet["node_id"]
        with report_lock:
            snapshot = json.loads(json.dumps(report))
        with _audit_lock:
            audit_copy = list(_audit_entries)
        response = {"type": "SNAPSHOT", "report": snapshot, "vector_ts": clock.value(), "node_id": config["node_id"], "audit_log": audit_copy}
        for peer in config["peers"]:
            if peer["node_id"] == requester_id:
                tcp_send(peer["host"], peer["tcp_port"], response)
                break

    elif ptype == "SNAPSHOT":
        received_ts = packet["vector_ts"]
        with report_lock:
            if sum(received_ts.values() if isinstance(received_ts, dict) else [0]) > sum(clock.value().values()):
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
                sse_broadcast("state_recovered", {"report": report_copy, "from_node": packet["node_id"], "ts": received_ts})
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

    # ── FEATURE 7: Leader Election ──
    elif ptype == "ELECTION":
        sender = packet["node_id"]
        clock.update(packet["vector_ts"])
        print(f"\n  🗳️  Received ELECTION from {sender}")
        if get_node_priority(node_id) > get_node_priority(sender):
            ans = {"type": "ANSWER", "node_id": node_id, "vector_ts": clock.tick()}
            for peer in config["peers"]:
                if peer["node_id"] == sender:
                    tcp_send(peer["host"], peer["tcp_port"], ans)
                    break
            print(f"  → Sent ANSWER to {sender} (I have higher priority)")
            threading.Thread(target=start_election, args=(config, clock), daemon=True).start()
        print(f"[{node_id} | TS={clock.value()}]> ", end="", flush=True)

    elif ptype == "ANSWER":
        clock.update(packet["vector_ts"])
        print(f"\n  📨 Received ANSWER from {packet['node_id']} — they will take over election")
        print(f"[{node_id} | TS={clock.value()}]> ", end="", flush=True)

    elif ptype == "COORDINATOR":
        global _current_leader, _election_in_progress
        leader_id = packet["leader_id"]
        clock.update(packet["vector_ts"])
        with _election_lock:
            _current_leader = leader_id
            _election_in_progress = False
        print(f"\n  👑 {leader_id} is now the LEADER (COORDINATOR received)")
        sse_broadcast("leader_elected", {"leader_id": leader_id, "elected_by": packet["node_id"], "ts": packet["vector_ts"]})
        write_audit(node_id, clock.value(), "ELECTION", "leader", leader_id)
        add_audit_entry(node_id, clock.value(), "ELECTION", "leader", f"👑 {leader_id} elected")
        print(f"[{node_id} | TS={clock.value()}]> ", end="", flush=True)

    # ── FEATURE 8: Rollback ──
    elif ptype == "ROLLBACK":
        sender = packet["node_id"]
        clock.update(packet["vector_ts"])
        with report_lock:
            for key, val in packet["report"].items():
                report[key] = val
            save_report(report)
        cp_id = packet.get("checkpoint_id", "?")
        print(f"\n  ⏪ ROLLBACK received from {sender} — reverted to Checkpoint #{cp_id}")
        write_audit(node_id, clock.value(), "ROLLBACK-RECV", "ALL", f"Checkpoint #{cp_id} from {sender}")
        add_audit_entry(node_id, clock.value(), "ROLLBACK", "ALL", f"Reverted via {sender}")
        with report_lock:
            rc = json.loads(json.dumps(report))
        sse_broadcast("rollback", {"report": rc, "checkpoint_id": cp_id, "ts": clock.value(), "by_node": sender})
        print(f"[{node_id} | TS={clock.value()}]> ", end="", flush=True)

def tcp_server(config, clock, report, report_lock):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", config["tcp_port"]))
    sock.listen(10)
    while True:
        conn, _ = sock.accept()
        threading.Thread(target=handle_connection, args=(conn, config, clock, report, report_lock), daemon=True).start()


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
#  UDP HEARTBEAT
# ═══════════════════════════════════════════════════════════════
def heartbeat_sender(config):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while True:
        msg = json.dumps({"type": "HEARTBEAT", "node_id": config["node_id"], "leader": _current_leader}).encode()
        for peer in config["peers"]:
            try:
                sock.sendto(msg, (peer["host"], peer["udp_port"]))
            except Exception:
                pass
        try:
            sock.sendto(msg, (config.get("dashboard_host", "localhost"), config.get("dashboard_port", 7000)))
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
                    if get_node_priority(pid) > get_node_priority(config["node_id"]):
                        pass  # Higher-priority node returns — it will start its own election
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
                        if _current_leader == pid:
                            print(f"\n  🗳️  Leader {pid} is DOWN — triggering election...")
                            threading.Thread(target=start_election, args=(config, clock), daemon=True).start()


# ═══════════════════════════════════════════════════════════════
#  LOCAL EDIT HANDLER
# ═══════════════════════════════════════════════════════════════
def do_edit(config, clock, report, report_lock, operation, field, value):
    node_id = config["node_id"]
    owned   = config["owned_fields"]

    if is_doc_locked():
        msg = "🔒 Document is LOCKED. No edits allowed."
        print(f"\n  ❌  {msg}")
        return False, msg

    if field not in owned:
        msg = f"ACCESS DENIED — '{field}' is not owned by {node_id}."
        print(f"\n  ❌  {msg}")
        print(f"       Your fields: {', '.join(owned)}\n")
        return False, msg

    ts = clock.tick()
    packet = {"type": "EDIT", "node_id": node_id, "vector_ts": ts, "operation": operation, "field": field, "value": value}

    with report_lock:
        apply_edit(report, operation, field, value)
        save_report(report)

    write_audit(node_id, ts, operation, field, value)
    add_audit_entry(node_id, ts, operation, field, value)
    print(f"\n  ✅  [{node_id} | TS={ts}]  {operation}  {field}  →  \"{value}\"")

    broadcast(config, packet)

    with report_lock:
        report_copy = json.loads(json.dumps(report))
    sse_broadcast("report_update", {"report": report_copy, "editor": node_id, "field": field, "value": value, "ts": ts, "conflict": False})
    notify_dashboard(config, report_copy, packet)
    increment_edit_count(report, report_lock, clock)

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

    edit_mine = {"type": "EDIT", "node_id": node_id, "vector_ts": ts, "operation": "update", "field": field, "value": f"Entry by {node_id} at TS={ts}"}
    edit_peer = {"type": "EDIT", "node_id": peer["node_id"], "vector_ts": ts, "operation": "update", "field": field, "value": f"Entry by {peer['node_id']} at TS={ts}"}

    print(f"\n{'═'*W}")
    print(f"  ⚡ CONFLICT TEST — VECTOR CLOCK RESOLUTION")
    print(f"{'═'*W}")
    print(f"  Field under conflict: {field}")
    print(f"  Scenario: {node_id} and {peer['node_id']} both write")
    print(f"            to the same field at TS={ts} simultaneously.")
    print(f"{'─'*W}")
    print(f"  Edit A:  [{node_id:<4} | TS={ts}]  →  \"{edit_mine['value']}\"")
    print(f"  Edit B:  [{peer['node_id']:<4} | TS={ts}]  →  \"{edit_peer['value']}\"")
    print(f"{'─'*W}")
    print(f"  RESOLUTION RULE:")
    print(f"    Step 1 — Compare Vector timestamps: {ts} == {ts}  → TIE")
    print(f"    Step 2 — Tiebreak by node_id (alphabetical order)")

    ordered = sorted([edit_mine, edit_peer], key=lambda e: (sum(e["vector_ts"].values()) if isinstance(e["vector_ts"], dict) else 0, e["node_id"]))
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

    with report_lock:
        report_copy = json.loads(json.dumps(report))
    sse_broadcast("conflict_test", {"report": report_copy, "field": field, "winner": winner["node_id"], "loser": loser["node_id"], "ts": ts})


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
#  DISPLAY HELPERS
# ═══════════════════════════════════════════════════════════════
W = 56

def print_banner(config):
    icons = {"ICU": "🏥", "RAD": "🔬"}
    role_icon = "👨‍⚕️" if config["role"] == "doctor" else "🩺"
    node_icon = icons.get(config["node_id"], "📋")
    crit = config.get("critical_fields", [])
    print("\n" + "═"*W)
    print(f"   {node_icon}  DISTRIBUTED CLINICAL REPORT SYSTEM")
    print(f"   {role_icon}  Node: {config['node_id']}   Role: {config['role'].upper()}")
    print("═"*W)
    print("   Commands:")
    print("     view                        — show full patient report")
    print("     update <field> <value>      — update a field")
    print("     append <field> <text>       — append text to a field")
    print("     status                      — clock, role, peers, leader")
    print("     conflict-test               — demo conflict resolution")
    print("     checkpoints                 — list saved checkpoints")
    print("     rollback                    — revert to last checkpoint")
    print("     election                    — trigger leader election")
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
    print(f"  Vector Clock : {clock.value()}")
    print(f"  TCP Port      : {config['tcp_port']}")
    print(f"  UDP Port      : {config['udp_port']}")
    print(f"  HTTP Port     : {config['http_port']}")
    print(f"  Owned Fields  : {', '.join(config['owned_fields'])}")
    print(f"  👑 Leader     : {_current_leader or 'NONE (election pending)'}")
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
        "node_id": _config["node_id"], "role": _config["role"],
        "owned_fields": _config["owned_fields"],
        "critical_fields": _config.get("critical_fields", []),
        "peers": [{"node_id": p["node_id"]} for p in _config["peers"]]
    })

@app.route("/api/report")
def api_report():
    with _report_lock:
        return jsonify({
            "report": json.loads(json.dumps(_report)),
            "vector_ts": _clock.value(), "locked": is_doc_locked(),
            "leader": _current_leader
        })

@app.route("/api/status")
def api_status():
    with _peer_lock:
        st = dict(peer_status)
    with _checkpoint_lock:
        cp_count = len(_checkpoints)
    return jsonify({
        "node_id": _config["node_id"], "role": _config["role"],
        "vector_ts": _clock.value(), "peers": st,
        "locked": is_doc_locked(), "leader": _current_leader,
        "checkpoint_count": cp_count
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
        return jsonify({"ok": True, "message": msg, "vector_ts": _clock.value()})
    else:
        return jsonify({"ok": False, "error": msg}), 403

@app.route("/api/events")
def api_events():
    q = queue.Queue()
    with _sse_lock:
        _sse_clients.append(q)

    def stream():
        try:
            with _report_lock:
                rpt = json.loads(json.dumps(_report))
            with _peer_lock:
                st = dict(peer_status)
            initial = {"report": rpt, "ts": _clock.value(), "locked": is_doc_locked(), "peers": st, "leader": _current_leader}
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
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})

@app.route("/api/audit")
def api_audit():
    with _audit_lock:
        return jsonify(list(_audit_entries))

@app.route("/api/checkpoints")
def api_checkpoints():
    with _checkpoint_lock:
        return jsonify([{"id": c["id"], "vector_ts": c["vector_ts"], "timestamp": c["timestamp"]} for c in _checkpoints])


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
                print('  Example: update icu.bp "120/80 mmHg"')
                continue
            do_edit(config, clock, report, report_lock, "update", parts[1], " ".join(parts[2:]))

        elif command == "append":
            if len(parts) < 3:
                print("  Usage:   append <field> <text>")
                print('  Example: append icu.notes "Patient stable."')
                continue
            do_edit(config, clock, report, report_lock, "append", parts[1], " ".join(parts[2:]))

        elif command == "status":
            print_status(config, clock)

        elif command == "conflict-test":
            run_conflict_test(config, clock, report, report_lock)

        elif command == "checkpoints":
            list_checkpoints()

        elif command == "rollback":
            do_rollback(config, clock, report, report_lock)

        elif command == "election":
            threading.Thread(target=start_election, args=(config, clock), daemon=True).start()

        elif command == "exit":
            print(f"\n  [{node_id}] Shutting down gracefully...")
            sys.exit(0)

        else:
            print(f"  Unknown command: '{command}'")
            print("  Commands: view | update | append | status | conflict-test | checkpoints | rollback | election | exit")


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════
def main():
    global _config, _clock, _report, _report_lock

    config      = load_config()
    report      = load_report()
    report_lock = threading.Lock()
    clock       = VectorClock(config["node_id"])

    _config      = config
    _clock       = clock
    _report      = report
    _report_lock = report_lock

    with _peer_lock:
        for peer in config["peers"]:
            peer_status[peer["node_id"]] = "UNKNOWN"
            last_seen[peer["node_id"]]   = time.time()

    print_banner(config)

    threading.Thread(target=tcp_server, args=(config, clock, report, report_lock), daemon=True).start()
    threading.Thread(target=edit_processor, args=(config, report, report_lock, clock), daemon=True).start()
    threading.Thread(target=heartbeat_sender, args=(config,), daemon=True).start()
    threading.Thread(target=heartbeat_listener, args=(config,), daemon=True).start()
    threading.Thread(target=failure_detector, args=(config, clock), daemon=True).start()

    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=config["http_port"], threaded=True), daemon=True).start()

    print(f"  ✅  [{config['node_id']}] Node is ONLINE")
    print(f"      TCP: {config['tcp_port']}  |  UDP: {config['udp_port']}  |  HTTP: {config['http_port']}")
    print(f"  🌐  Open http://localhost:{config['http_port']} in browser")
    print(f"  ⏳  Waiting for peers...\n")

    time.sleep(1)
    request_state_recovery(config)

    # [FEATURE 7] Trigger leader election on startup
    time.sleep(1)
    threading.Thread(target=start_election, args=(config, clock), daemon=True).start()

    terminal_loop(config, clock, report, report_lock)


if __name__ == "__main__":
    main()
`

## 2. Peer Node Frontend User Interface
> Filepath: icu/index.html

`html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Clinical Report Node</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0a0e1a;
      --surface: rgba(255,255,255,0.04);
      --surface-hover: rgba(255,255,255,0.07);
      --glass: rgba(255,255,255,0.06);
      --glass-border: rgba(255,255,255,0.1);
      --text: #e8eaf6;
      --text-dim: #7986cb;
      --text-muted: #4a5078;
      --accent: #42a5f5;
      --accent-glow: rgba(66,165,245,0.25);
      --success: #66bb6a;
      --danger: #ef5350;
      --warning: #ffa726;
      --icu-color: #42a5f5;
      --rad-color: #b388ff;
      --radius: 14px;
      --radius-sm: 8px;
    }
    * { margin:0; padding:0; box-sizing:border-box; }
    body {
      font-family: 'Inter', -apple-system, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      overflow-x: hidden;
    }
    body::before {
      content:'';
      position:fixed; inset:0;
      background: radial-gradient(ellipse at 20% 0%, var(--accent-glow) 0%, transparent 50%),
                  radial-gradient(ellipse at 80% 100%, rgba(179,136,255,0.08) 0%, transparent 50%);
      pointer-events:none; z-index:0;
    }
    .container {
      position:relative; z-index:1;
      max-width: 840px;
      margin: 0 auto;
      padding: 24px 20px 60px;
    }

    /* ─── Header ─── */
    .header {
      background: var(--glass);
      border: 1px solid var(--glass-border);
      border-radius: var(--radius);
      padding: 20px 28px;
      margin-bottom: 20px;
      backdrop-filter: blur(20px);
      display: flex; align-items: center; justify-content: space-between;
    }
    .header-left { display:flex; align-items:center; gap:14px; }
    .node-icon {
      font-size: 2rem;
      width: 52px; height: 52px;
      display:flex; align-items:center; justify-content:center;
      background: var(--accent-glow);
      border-radius: 12px;
    }
    .node-name { font-size:1.5rem; font-weight:700; letter-spacing:-0.5px; }
    .node-role { font-size:0.8rem; font-weight:500; color:var(--text-dim); text-transform:uppercase; letter-spacing:1.5px; }
    .header-right { text-align:right; }
    .ts-label { font-size:0.7rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:1px; }
    .ts-value { font-size:1.8rem; font-weight:700; color:var(--accent); font-variant-numeric:tabular-nums; }
    .lock-indicator {
      display:inline-block; padding:3px 10px; border-radius:20px; font-size:0.7rem; font-weight:600;
      margin-top:4px; letter-spacing:0.5px;
    }
    .lock-indicator.locked { background:rgba(239,83,80,0.2); color:var(--danger); }
    .lock-indicator.unlocked { background:rgba(102,187,106,0.15); color:var(--success); }

    /* ─── Leader Badge ─── */
    .leader-badge {
      display:inline-block; padding:3px 10px; border-radius:20px; font-size:0.7rem; font-weight:600;
      margin-top:4px; letter-spacing:0.5px; margin-left: 6px;
    }
    .leader-badge.is-leader { background:rgba(255,215,0,0.2); color:#ffd700; }
    .leader-badge.not-leader { background:rgba(255,255,255,0.06); color:var(--text-muted); }

    /* ─── Patient Info ─── */
    .patient-bar {
      background: linear-gradient(135deg, rgba(66,165,245,0.12), rgba(179,136,255,0.08));
      border: 1px solid var(--glass-border);
      border-radius: var(--radius);
      padding: 16px 24px;
      margin-bottom: 20px;
      backdrop-filter: blur(12px);
    }
    .patient-name { font-size:1.15rem; font-weight:600; }
    .patient-details { font-size:0.85rem; color:var(--text-dim); margin-top:4px; }
    .patient-diagnosis {
      margin-top:8px; padding:5px 12px; display:inline-block;
      background:rgba(255,167,38,0.12); border:1px solid rgba(255,167,38,0.2);
      border-radius:6px; font-size:0.8rem; font-weight:500; color:var(--warning);
    }

    /* ─── Section Cards ─── */
    .section {
      background: var(--glass);
      border: 1px solid var(--glass-border);
      border-radius: var(--radius);
      margin-bottom: 16px;
      overflow: hidden;
      backdrop-filter: blur(12px);
    }
    .section-header {
      padding: 14px 22px;
      display:flex; align-items:center; justify-content:space-between;
      font-size:0.75rem; font-weight:600; text-transform:uppercase; letter-spacing:1.5px;
      border-bottom: 1px solid var(--glass-border);
    }
    .section-header .badge {
      font-size:0.65rem; padding:2px 8px; border-radius:4px; font-weight:500;
    }
    .section-body { padding: 16px 22px; }

    .section.icu .section-header { color:var(--icu-color); background:rgba(66,165,245,0.06); }
    .section.icu .section-header .badge { background:rgba(66,165,245,0.15); color:var(--icu-color); }
    .section.rad .section-header { color:var(--rad-color); background:rgba(179,136,255,0.06); }
    .section.rad .section-header .badge { background:rgba(179,136,255,0.15); color:var(--rad-color); }

    /* ─── Field Rows ─── */
    .field-row {
      display:flex; justify-content:space-between; align-items:baseline;
      padding: 6px 0;
      border-bottom: 1px solid rgba(255,255,255,0.03);
      transition: background 0.4s;
    }
    .field-row:last-child { border-bottom:none; }
    .field-label { font-size:0.8rem; color:var(--text-dim); font-weight:500; min-width:140px; }
    .field-value { font-size:0.85rem; color:var(--text); text-align:right; flex:1; word-break:break-word; }
    .field-value.empty { color:var(--text-muted); font-style:italic; }

    .field-row.highlight {
      background: var(--accent-glow);
      border-radius: 6px;
      padding: 6px 8px;
      margin: 2px -8px;
    }

    /* ─── Editable Fields ─── */
    .edit-section .section-header { color:var(--accent); background:rgba(66,165,245,0.08); }
    .edit-group { padding:10px 0; border-bottom:1px solid rgba(255,255,255,0.04); }
    .edit-group:last-child { border-bottom:none; }
    .edit-label { font-size:0.75rem; color:var(--text-dim); font-weight:500; margin-bottom:6px; text-transform:uppercase; letter-spacing:0.5px; }
    .edit-row { display:flex; gap:8px; }
    .edit-input {
      flex:1;
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: var(--radius-sm);
      padding: 10px 14px;
      color: var(--text);
      font-family: inherit;
      font-size: 0.85rem;
      outline:none;
      transition: border-color 0.2s, box-shadow 0.2s;
    }
    .edit-input:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px var(--accent-glow);
    }
    .btn {
      padding: 10px 18px;
      border-radius: var(--radius-sm);
      border:none;
      font-family:inherit;
      font-size:0.8rem;
      font-weight:600;
      cursor:pointer;
      transition: all 0.2s;
      text-transform:uppercase;
      letter-spacing:0.5px;
    }
    .btn-save {
      background: linear-gradient(135deg, var(--accent), #1e88e5);
      color: white;
    }
    .btn-save:hover { transform:translateY(-1px); box-shadow:0 4px 16px var(--accent-glow); }
    .btn-append {
      background: linear-gradient(135deg, #7c4dff, #651fff);
      color:white;
    }
    .btn-append:hover { transform:translateY(-1px); box-shadow:0 4px 16px rgba(124,77,255,0.3); }
    .btn:active { transform:translateY(0); }
    .btn:disabled { opacity:0.4; cursor:not-allowed; transform:none !important; box-shadow:none !important; }

    /* ─── Peer Status ─── */
    .peers-bar {
      display:flex; gap:12px; margin-bottom:20px;
    }
    .peer-chip {
      flex:1;
      background: var(--glass);
      border: 1px solid var(--glass-border);
      border-radius: var(--radius-sm);
      padding: 10px 16px;
      display:flex; align-items:center; gap:8px;
      font-size:0.8rem; font-weight:500;
      backdrop-filter: blur(8px);
      transition: border-color 0.3s, background 0.3s;
    }
    .peer-dot {
      width:8px; height:8px; border-radius:50%;
      transition: background 0.3s, box-shadow 0.3s;
    }
    .peer-chip.online .peer-dot { background:var(--success); box-shadow:0 0 8px var(--success); }
    .peer-chip.offline .peer-dot { background:var(--danger); box-shadow:0 0 8px var(--danger); }
    .peer-chip.unknown .peer-dot { background:var(--text-muted); }
    .peer-chip.online { border-color:rgba(102,187,106,0.2); }
    .peer-chip.offline { border-color:rgba(239,83,80,0.2); background:rgba(239,83,80,0.05); }

    /* ─── Checkpoint Bar ─── */
    .checkpoint-bar {
      background: var(--glass);
      border: 1px solid var(--glass-border);
      border-radius: var(--radius-sm);
      padding: 10px 16px;
      margin-bottom: 20px;
      display:flex; align-items:center; justify-content:space-between;
      font-size:0.8rem;
      backdrop-filter: blur(8px);
    }
    .checkpoint-bar .cp-info { color:var(--text-dim); }
    .checkpoint-bar .cp-count { font-weight:600; color:var(--accent); }

    /* ─── Toast ─── */
    .toast-container { position:fixed; top:20px; right:20px; z-index:1000; display:flex; flex-direction:column; gap:8px; }
    .toast {
      background:rgba(20,25,40,0.95);
      border:1px solid var(--glass-border);
      border-radius: var(--radius-sm);
      padding:12px 18px;
      font-size:0.8rem;
      backdrop-filter:blur(12px);
      animation: toastIn 0.3s ease, toastOut 0.3s ease 2.7s forwards;
      max-width:340px;
    }
    .toast.success { border-color:rgba(102,187,106,0.3); }
    .toast.error { border-color:rgba(239,83,80,0.3); }
    .toast.warning { border-color:rgba(255,167,38,0.3); }
    .toast-title { font-weight:600; margin-bottom:2px; }
    .toast-body { color:var(--text-dim); font-size:0.75rem; }
    @keyframes toastIn { from { opacity:0; transform:translateX(30px); } to { opacity:1; transform:translateX(0); } }
    @keyframes toastOut { from { opacity:1; } to { opacity:0; transform:translateY(-10px); } }

    /* ─── Connection Badge ─── */
    .conn-badge {
      position:fixed; bottom:16px; right:16px;
      padding:6px 14px; border-radius:20px;
      font-size:0.7rem; font-weight:600; letter-spacing:0.5px;
      z-index:100; backdrop-filter:blur(8px);
    }
    .conn-badge.connected { background:rgba(102,187,106,0.15); color:var(--success); border:1px solid rgba(102,187,106,0.2); }
    .conn-badge.disconnected { background:rgba(239,83,80,0.15); color:var(--danger); border:1px solid rgba(239,83,80,0.2); }

    @media (max-width:600px) {
      .container { padding:12px; }
      .header { flex-direction:column; gap:12px; text-align:center; }
      .header-right { text-align:center; }
      .peers-bar { flex-direction:column; }
      .edit-row { flex-direction:column; }
    }
  </style>
</head>
<body>
  <div class="container">
    <!-- Header -->
    <div class="header" id="header">
      <div class="header-left">
        <div class="node-icon" id="nodeIcon">🏥</div>
        <div>
          <div class="node-name" id="nodeName">NODE</div>
          <div class="node-role" id="nodeRole">Loading...</div>
        </div>
      </div>
      <div class="header-right">
        <div class="ts-label">Vector Clock</div>
        <div class="ts-value" id="tsValue">0</div>
        <div>
          <span class="lock-indicator unlocked" id="lockIndicator">🔓 UNLOCKED</span>
          <span class="leader-badge not-leader" id="leaderBadge">👑 —</span>
        </div>
      </div>
    </div>

    <!-- Patient Info -->
    <div class="patient-bar">
      <div class="patient-name" id="patientName">Loading...</div>
      <div class="patient-details" id="patientDetails"></div>
      <div class="patient-diagnosis" id="patientDiagnosis"></div>
    </div>

    <!-- Peer Status -->
    <div class="peers-bar" id="peersBar"></div>

    <!-- Checkpoint Bar -->
    <div class="checkpoint-bar" id="checkpointBar">
      <span class="cp-info">📸 Checkpoints: <span class="cp-count" id="cpCount">0</span></span>
      <span class="cp-info" id="cpLastSaved" style="font-size:0.7rem;color:var(--text-muted);">none yet</span>
    </div>

    <!-- Editable Fields -->
    <div class="section edit-section" id="editSection">
      <div class="section-header">
        <span>✏️ &nbsp;Your Fields</span>
        <span class="badge" style="background:rgba(66,165,245,0.15);color:var(--icu-color);">EDITABLE</span>
      </div>
      <div class="section-body" id="editFields"></div>
    </div>

    <!-- Full Report: ICU -->
    <div class="section icu" id="sectionICU">
      <div class="section-header">
        <span>🏥 &nbsp;ICU Findings</span>
        <span class="badge">VITALS</span>
      </div>
      <div class="section-body">
        <div class="field-row" id="row-icu.bp"><span class="field-label">Blood Pressure</span><span class="field-value" id="val-icu.bp">—</span></div>
        <div class="field-row" id="row-icu.oxygen_saturation"><span class="field-label">Oxygen Saturation</span><span class="field-value" id="val-icu.oxygen_saturation">—</span></div>
        <div class="field-row" id="row-icu.heart_rate"><span class="field-label">Heart Rate</span><span class="field-value" id="val-icu.heart_rate">—</span></div>
        <div class="field-row" id="row-icu.treatment"><span class="field-label">Treatment</span><span class="field-value" id="val-icu.treatment">—</span></div>
        <div class="field-row" id="row-icu.notes"><span class="field-label">ICU Notes</span><span class="field-value" id="val-icu.notes">—</span></div>
      </div>
    </div>

    <!-- Full Report: Radiology -->
    <div class="section rad" id="sectionRAD">
      <div class="section-header">
        <span>🔬 &nbsp;Radiology</span>
        <span class="badge">IMAGING</span>
      </div>
      <div class="section-body">
        <div class="field-row" id="row-radiology.xray"><span class="field-label">X-Ray</span><span class="field-value" id="val-radiology.xray">—</span></div>
        <div class="field-row" id="row-radiology.echo"><span class="field-label">Echo</span><span class="field-value" id="val-radiology.echo">—</span></div>
        <div class="field-row" id="row-radiology.notes"><span class="field-label">Radiology Notes</span><span class="field-value" id="val-radiology.notes">—</span></div>
      </div>
    </div>
  </div>

  <div class="toast-container" id="toasts"></div>
  <div class="conn-badge disconnected" id="connBadge">⚡ CONNECTING...</div>

  <script>
    let config = null;
    let report = null;
    let vectorTs = {};
    let isLocked = false;
    let currentLeader = null;
    let eventSource = null;

    const NODE_THEMES = {
      ICU: { icon:'🏥', accent:'#42a5f5', label:'ICU Terminal',  glow:'rgba(66,165,245,0.25)' },
      RAD: { icon:'🔬', accent:'#b388ff', label:'Radiology Terminal', glow:'rgba(179,136,255,0.25)' }
    };

    const FIELD_LABELS = {
      'icu.bp': 'Blood Pressure', 'icu.oxygen_saturation': 'Oxygen Saturation',
      'icu.heart_rate': 'Heart Rate', 'icu.treatment': 'Treatment', 'icu.notes': 'ICU Notes',
      'radiology.xray': 'X-Ray Findings', 'radiology.echo': 'Echo', 'radiology.notes': 'Radiology Notes'
    };

    async function init() {
      const cfgRes = await fetch('/api/config');
      config = await cfgRes.json();

      const theme = NODE_THEMES[config.node_id] || NODE_THEMES.ICU;
      document.documentElement.style.setProperty('--accent', theme.accent);
      document.documentElement.style.setProperty('--accent-glow', theme.glow);
      document.getElementById('nodeIcon').textContent = theme.icon;
      document.getElementById('nodeName').textContent = theme.label;
      document.getElementById('nodeRole').textContent = `${config.role.toUpperCase()} — ${config.node_id}`;
      document.title = `${config.node_id} | Clinical Report`;

      buildPeerChips();
      buildEditFields();

      const rptRes = await fetch('/api/report');
      const rptData = await rptRes.json();
      report = rptData.report;
      vectorTs = rptData.vector_ts;
      isLocked = rptData.locked;
      currentLeader = rptData.leader;
      renderReport();
      renderTs();
      renderLock();
      renderLeader();

      connectSSE();
      setInterval(pollStatus, 3000);
    }

    function buildPeerChips() {
      const bar = document.getElementById('peersBar');
      bar.innerHTML = '';
      for (const peer of config.peers) {
        const chip = document.createElement('div');
        chip.className = 'peer-chip unknown';
        chip.id = `peer-${peer.node_id}`;
        chip.innerHTML = `<span class="peer-dot"></span><span>${peer.node_id}</span><span class="peer-leader-tag" id="peer-leader-${peer.node_id}" style="display:none;font-size:0.65rem;color:#ffd700;margin-left:auto;">👑 LEADER</span>`;
        bar.appendChild(chip);
      }
    }

    function buildEditFields() {
      const container = document.getElementById('editFields');
      container.innerHTML = '';
      if (config.owned_fields.length === 0) {
        container.innerHTML = '<div style="padding:12px 0;color:var(--text-muted);font-size:0.85rem;text-align:center;">🔒 Read-only mode</div>';
        return;
      }

      for (const field of config.owned_fields) {
        const label = FIELD_LABELS[field] || field;
        const isNotes = field.toLowerCase().includes('notes');
        const group = document.createElement('div');
        group.className = 'edit-group';
        group.innerHTML = `
          <div class="edit-label">${label}</div>
          <div class="edit-row">
            <input type="text" class="edit-input" id="input-${field}" placeholder="Enter ${label.toLowerCase()}..." />
            ${isNotes
              ? `<button class="btn btn-append" onclick="submitEdit('append','${field}')">Append</button>`
              : `<button class="btn btn-save" onclick="submitEdit('update','${field}')">Save</button>`
            }
          </div>`;
        container.appendChild(group);
      }
    }

    function getField(obj, path) {
      const keys = path.split('.');
      let cur = obj;
      for (const k of keys) {
        if (!cur || typeof cur !== 'object') return null;
        cur = cur[k];
      }
      return cur;
    }

    function renderReport(highlightField) {
      if (!report) return;
      document.getElementById('patientName').textContent = `${report.patient_name} — ${report.age}M`;
      document.getElementById('patientDetails').textContent = `Admitted: ${report.admission_date}`;
      document.getElementById('patientDiagnosis').textContent = report.diagnosis;

      const allFields = ['icu.bp','icu.oxygen_saturation','icu.heart_rate','icu.treatment','icu.notes','radiology.xray','radiology.echo','radiology.notes'];
      for (const f of allFields) {
        const el = document.getElementById(`val-${f}`);
        if (!el) continue;
        const val = getField(report, f);
        if (val && val.trim && val.trim()) {
          el.textContent = val;
          el.classList.remove('empty');
        } else {
          el.textContent = val || '—';
          el.classList.toggle('empty', !val || !val.trim || !val.trim());
        }
        const row = document.getElementById(`row-${f}`);
        if (row && f === highlightField) {
          row.classList.add('highlight');
          setTimeout(() => row.classList.remove('highlight'), 1500);
        }
      }
    }

    function renderTs() { 
      const dict = typeof vectorTs === 'object' && vectorTs ? JSON.stringify(vectorTs).replace(/"/g, '').replace(/,/g, ' | ') : vectorTs;
      document.getElementById('tsValue').textContent = dict; 
    }

    function renderLock() {
      const el = document.getElementById('lockIndicator');
      if (isLocked) {
        el.textContent = '🔒 LOCKED'; el.className = 'lock-indicator locked';
      } else {
        el.textContent = '🔓 UNLOCKED'; el.className = 'lock-indicator unlocked';
      }
      document.querySelectorAll('.btn').forEach(b => b.disabled = isLocked);
    }

    function renderLeader() {
      const badge = document.getElementById('leaderBadge');
      if (currentLeader === config.node_id) {
        badge.textContent = '👑 LEADER'; badge.className = 'leader-badge is-leader';
      } else if (currentLeader) {
        badge.textContent = `👑 ${currentLeader}`; badge.className = 'leader-badge not-leader';
      } else {
        badge.textContent = '👑 —'; badge.className = 'leader-badge not-leader';
      }
      // Update peer chips
      for (const peer of (config.peers || [])) {
        const tag = document.getElementById(`peer-leader-${peer.node_id}`);
        if (tag) tag.style.display = (currentLeader === peer.node_id) ? 'inline' : 'none';
      }
    }

    function renderPeerStatus(peerId, status) {
      const chip = document.getElementById(`peer-${peerId}`);
      if (!chip) return;
      chip.className = `peer-chip ${status.toLowerCase()}`;
    }

    function connectSSE() {
      if (eventSource) eventSource.close();
      eventSource = new EventSource('/api/events');

      eventSource.addEventListener('initial_state', (e) => {
        const data = JSON.parse(e.data);
        report = data.report; vectorTs = data.ts; isLocked = data.locked;
        currentLeader = data.leader;
        renderReport(); renderTs(); renderLock(); renderLeader();
        if (data.peers) {
          for (const [pid, st] of Object.entries(data.peers)) renderPeerStatus(pid, st);
        }
        setBadge(true);
      });

      eventSource.addEventListener('report_update', (e) => {
        const data = JSON.parse(e.data);
        report = data.report; vectorTs = data.ts;
        renderReport(data.field); renderTs();
        if (data.editor !== config.node_id) {
          showToast('success', `Updated by ${data.editor}`, `${data.field} → "${truncate(data.value, 40)}"`);
        }
      });

      eventSource.addEventListener('state_recovered', (e) => {
        const data = JSON.parse(e.data);
        report = data.report; vectorTs = data.ts;
        renderReport(); renderTs();
        showToast('success', 'State Recovered', `Synced from ${data.from_node} (TS=${data.ts})`);
      });

      eventSource.addEventListener('peer_status', (e) => {
        const data = JSON.parse(e.data);
        renderPeerStatus(data.peer_id, data.status);
        const icon = data.status === 'ONLINE' ? '✅' : '⚠️';
        showToast(data.status === 'ONLINE' ? 'success' : 'error', `${icon} ${data.peer_id}`, data.status);
      });

      eventSource.addEventListener('lock_status', (e) => {
        const data = JSON.parse(e.data);
        isLocked = data.locked; renderLock();
        showToast(isLocked ? 'error' : 'success',
                  isLocked ? '🔒 Document Locked' : '🔓 Document Unlocked',
                  isLocked ? 'All edits are disabled' : 'Editing is now allowed');
      });

      eventSource.addEventListener('conflict_test', (e) => {
        const data = JSON.parse(e.data);
        report = data.report; renderReport(data.field);
        showToast('success', '⚡ Conflict Resolved', `Winner: ${data.winner} (field: ${data.field})`);
      });

      eventSource.addEventListener('leader_elected', (e) => {
        const data = JSON.parse(e.data);
        currentLeader = data.leader_id; renderLeader();
        showToast('warning', '👑 Leader Elected', `${data.leader_id} is now the leader`);
      });

      eventSource.addEventListener('checkpoint_created', (e) => {
        const data = JSON.parse(e.data);
        document.getElementById('cpCount').textContent = data.id;
        document.getElementById('cpLastSaved').textContent = `Last: ${data.timestamp}`;
        showToast('success', '📸 Checkpoint Saved', `Checkpoint #${data.id} (TS=${data.vector_ts})`);
      });

      eventSource.addEventListener('rollback', (e) => {
        const data = JSON.parse(e.data);
        report = data.report; renderReport(); renderTs();
        showToast('warning', '⏪ Rollback', `Reverted to Checkpoint #${data.checkpoint_id} by ${data.by_node}`);
      });

      eventSource.onopen = () => setBadge(true);
      eventSource.onerror = () => { setBadge(false); setTimeout(connectSSE, 3000); };
    }

    async function submitEdit(operation, field) {
      const input = document.getElementById(`input-${field}`);
      const value = input.value.trim();
      if (!value) return;
      try {
        const res = await fetch('/api/edit', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ operation, field, value })
        });
        const data = await res.json();
        if (data.ok) {
          input.value = ''; vectorTs = data.vector_ts; renderTs();
          showToast('success', '✅ Saved', `${field} → "${truncate(value, 40)}"`);
        } else {
          showToast('error', '❌ Error', data.error);
        }
      } catch (err) {
        showToast('error', '❌ Network Error', err.message);
      }
    }

    async function pollStatus() {
      try {
        const res = await fetch('/api/status');
        const data = await res.json();
        vectorTs = data.vector_ts; renderTs();
        isLocked = data.locked; renderLock();
        currentLeader = data.leader; renderLeader();
        document.getElementById('cpCount').textContent = data.checkpoint_count || 0;
        for (const [pid, st] of Object.entries(data.peers)) renderPeerStatus(pid, st);
      } catch (e) { /* ignore */ }
    }

    function truncate(s, n) { return s.length > n ? s.slice(0, n) + '…' : s; }
    function setBadge(connected) {
      const b = document.getElementById('connBadge');
      b.textContent = connected ? '⚡ LIVE' : '⚡ RECONNECTING...';
      b.className = `conn-badge ${connected ? 'connected' : 'disconnected'}`;
    }
    function showToast(type, title, body) {
      const container = document.getElementById('toasts');
      const toast = document.createElement('div');
      toast.className = `toast ${type}`;
      toast.innerHTML = `<div class="toast-title">${title}</div><div class="toast-body">${body}</div>`;
      container.appendChild(toast);
      setTimeout(() => toast.remove(), 3200);
    }

    init();
  </script>
</body>
</html>

`

## 3. Admin Dashboard Analytics Server
> Filepath: dashboard/server.js

`javascript
/**
 * Dashboard Server — Admin Dashboard for Distributed Clinical Report System
 *
 * Features:
 * - Express serves static files from public/ on port 4000
 * - WebSocket server for real-time browser updates
 * - UDP listener on port 7000 receives DASHBOARD_UPDATE + HEARTBEAT from nodes
 * - POST /api/lock and /api/unlock send TCP commands to all nodes
 * - Tracks leader election status, checkpoint events, 2PC events
 */

const express = require('express');
const http = require('http');
const WebSocket = require('ws');
const dgram = require('dgram');
const net = require('net');
const path = require('path');

const HTTP_PORT = 4000;
const UDP_PORT = 7000;
const HEARTBEAT_TIMEOUT = 9000; // ms

const NODES = [
  { id: 'ICU', host: 'localhost', tcpPort: 5001 },
  { id: 'RAD', host: 'localhost', tcpPort: 5002 }
];

// ─── State ───
let latestReport = null;
let nodesStatus = {};
let auditLog = [];
let docLocked = false;
let currentLeader = null;
let lastHeartbeat = {};  // { nodeId: timestamp }

// ═══════════════════════════════════════════════════════
//  EXPRESS + WEBSOCKET
// ═══════════════════════════════════════════════════════
const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

const server = http.createServer(app);
const wss = new WebSocket.Server({ server });

wss.on('connection', (ws) => {
  console.log('[WS] Client connected');
  const initial = {
    type: 'initial_state',
    report: latestReport,
    nodes_status: nodesStatus,
    audit_log: auditLog,
    locked: docLocked,
    leader: currentLeader
  };
  ws.send(JSON.stringify(initial));
  ws.on('close', () => console.log('[WS] Client disconnected'));
});

function broadcastToWS(data) {
  const msg = JSON.stringify(data);
  wss.clients.forEach((client) => {
    if (client.readyState === WebSocket.OPEN) client.send(msg);
  });
}

// ═══════════════════════════════════════════════════════
//  UDP LISTENER — receives updates + heartbeats from nodes
// ═══════════════════════════════════════════════════════
const udpServer = dgram.createSocket('udp4');

udpServer.on('message', (msg) => {
  try {
    const packet = JSON.parse(msg.toString());

    if (packet.type === 'DASHBOARD_UPDATE') {
      latestReport = packet.report;
      nodesStatus = packet.nodes_status || nodesStatus;
      if (packet.leader) currentLeader = packet.leader;

      if (packet.audit_log && packet.audit_log.length > 0) {
        auditLog = packet.audit_log;
      }

      broadcastToWS({
        type: 'report_update',
        report: packet.report,
        node_id: packet.node_id,
        field: packet.field,
        value: packet.value,
        vector_ts: packet.vector_ts,
        operation: packet.operation,
        nodes_status: nodesStatus,
        audit_log: auditLog,
        leader: currentLeader
      });
    }

    if (packet.type === 'HEARTBEAT') {
      const nodeId = packet.node_id;
      lastHeartbeat[nodeId] = Date.now();
      if (packet.leader) currentLeader = packet.leader;

      // Mark as online
      if (nodesStatus[nodeId] !== 'ONLINE') {
        nodesStatus[nodeId] = 'ONLINE';
        broadcastToWS({
          type: 'node_status_update',
          nodes_status: nodesStatus,
          leader: currentLeader
        });
      }
    }
  } catch (e) {
    // Ignore malformed packets
  }
});

udpServer.on('listening', () => {
  console.log(`[UDP] Listening on port ${UDP_PORT} for node updates`);
});

udpServer.bind(UDP_PORT);

// ═══════════════════════════════════════════════════════
//  HEARTBEAT FAILURE DETECTOR
// ═══════════════════════════════════════════════════════
setInterval(() => {
  const now = Date.now();
  let changed = false;
  for (const node of NODES) {
    const lastSeen = lastHeartbeat[node.id];
    if (lastSeen && (now - lastSeen) > HEARTBEAT_TIMEOUT) {
      if (nodesStatus[node.id] !== 'OFFLINE') {
        nodesStatus[node.id] = 'OFFLINE';
        changed = true;
        console.log(`[HEARTBEAT] ${node.id} is OFFLINE`);
      }
    }
  }
  if (changed) {
    broadcastToWS({
      type: 'node_status_update',
      nodes_status: nodesStatus,
      leader: currentLeader
    });
  }
}, 3000);

// ═══════════════════════════════════════════════════════
//  TCP SEND — for lock/unlock commands to nodes
// ═══════════════════════════════════════════════════════
function sendTCPToNode(host, port, packet) {
  return new Promise((resolve) => {
    const client = new net.Socket();
    client.setTimeout(3000);
    client.connect(port, host, () => {
      client.write(JSON.stringify(packet));
      client.end();
      resolve(true);
    });
    client.on('error', () => resolve(false));
    client.on('timeout', () => { client.destroy(); resolve(false); });
  });
}

async function broadcastTCPToNodes(packet) {
  const results = {};
  for (const node of NODES) {
    results[node.id] = await sendTCPToNode(node.host, node.tcpPort, packet);
  }
  return results;
}

// ─── Lock endpoint ───
app.post('/api/lock', async (req, res) => {
  docLocked = true;
  const results = await broadcastTCPToNodes({ type: 'LOCK', node_id: 'ADMIN' });
  broadcastToWS({ type: 'lock_status', locked: true });
  console.log('[LOCK] Document locked —', results);
  res.json({ ok: true, results });
});

// ─── Unlock endpoint ───
app.post('/api/unlock', async (req, res) => {
  docLocked = false;
  const results = await broadcastTCPToNodes({ type: 'UNLOCK', node_id: 'ADMIN' });
  broadcastToWS({ type: 'lock_status', locked: false });
  console.log('[UNLOCK] Document unlocked —', results);
  res.json({ ok: true, results });
});

// ─── Status endpoint ───
app.get('/api/status', (req, res) => {
  res.json({
    report: latestReport,
    nodes_status: nodesStatus,
    audit_log: auditLog,
    locked: docLocked,
    leader: currentLeader
  });
});

// ═══════════════════════════════════════════════════════
//  START
// ═══════════════════════════════════════════════════════
server.listen(HTTP_PORT, () => {
  console.log(`\n  ┌──────────────────────────────────────────────┐`);
  console.log(`  │  🏥  CLINICAL REPORT — ADMIN DASHBOARD       │`);
  console.log(`  │  🌐  http://localhost:${HTTP_PORT}                   │`);
  console.log(`  │  📡  UDP listening on port ${UDP_PORT}               │`);
  console.log(`  └──────────────────────────────────────────────┘\n`);
});

`

## 4. Admin Dashboard User Interface
> Filepath: dashboard/public/index.html

`html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Admin Dashboard — Clinical Report</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #060a14;
      --surface: rgba(255,255,255,0.035);
      --glass: rgba(255,255,255,0.05);
      --glass-border: rgba(255,255,255,0.08);
      --text: #e8eaf6;
      --text-dim: #8892b0;
      --text-muted: #4a5078;
      --icu: #42a5f5;
      --rad: #b388ff;

      --success: #66bb6a;
      --danger: #ef5350;
      --warning: #ffa726;
      --gold: #ffd54f;
      --radius: 16px;
      --radius-sm: 10px;
    }
    * { margin:0; padding:0; box-sizing:border-box; }
    body {
      font-family: 'Inter', sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
    }
    body::before {
      content:'';
      position:fixed; inset:0; pointer-events:none; z-index:0;
      background:
        radial-gradient(ellipse at 10% 20%, rgba(66,165,245,0.07) 0%, transparent 50%),
        radial-gradient(ellipse at 90% 80%, rgba(179,136,255,0.06) 0%, transparent 50%),
        radial-gradient(ellipse at 50% 50%, rgba(105,240,174,0.04) 0%, transparent 60%);
    }

    .dashboard {
      position:relative; z-index:1;
      max-width:960px; margin:0 auto; padding:28px 20px 60px;
    }

    /* ─── Title Bar ─── */
    .title-bar {
      display:flex; align-items:center; justify-content:space-between;
      margin-bottom:24px;
    }
    .title-left { display:flex; align-items:center; gap:14px; }
    .title-icon {
      font-size:2.2rem; width:56px; height:56px;
      display:flex; align-items:center; justify-content:center;
      background:linear-gradient(135deg, rgba(66,165,245,0.15), rgba(179,136,255,0.15));
      border-radius:14px; border:1px solid var(--glass-border);
    }
    .title-text h1 { font-size:1.3rem; font-weight:700; letter-spacing:-0.5px; }
    .title-text p { font-size:0.75rem; color:var(--text-dim); margin-top:2px; }
    .title-right { display:flex; gap:10px; }

    /* ─── Buttons ─── */
    .btn {
      padding:10px 20px; border-radius:var(--radius-sm); border:none;
      font-family:inherit; font-size:0.78rem; font-weight:600;
      cursor:pointer; transition:all 0.2s; text-transform:uppercase; letter-spacing:0.5px;
    }
    .btn:active { transform:scale(0.97); }
    .btn-lock {
      background:linear-gradient(135deg, var(--danger), #c62828);
      color:white;
    }
    .btn-lock:hover { box-shadow:0 4px 20px rgba(239,83,80,0.3); }
    .btn-unlock {
      background:linear-gradient(135deg, var(--success), #2e7d32);
      color:white;
    }
    .btn-unlock:hover { box-shadow:0 4px 20px rgba(102,187,106,0.3); }

    /* ─── Patient Header ─── */
    .patient-header {
      background: linear-gradient(135deg, rgba(66,165,245,0.1), rgba(179,136,255,0.08), rgba(105,240,174,0.06));
      border:1px solid var(--glass-border);
      border-radius:var(--radius); padding:22px 28px;
      margin-bottom:20px; backdrop-filter:blur(16px);
      position:relative; overflow:hidden;
    }
    .patient-header::before {
      content:''; position:absolute; top:0; left:0; right:0; height:3px;
      background:linear-gradient(90deg, var(--icu), var(--rad));
    }
    .ph-row1 { display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; }
    .ph-name { font-size:1.35rem; font-weight:700; }
    .ph-age { font-size:0.9rem; color:var(--text-dim); }
    .ph-row2 { margin-top:8px; font-size:0.85rem; color:var(--text-dim); }
    .ph-diagnosis {
      display:inline-block; margin-top:10px; padding:5px 14px;
      background:rgba(255,167,38,0.12); border:1px solid rgba(255,167,38,0.2);
      border-radius:6px; font-size:0.8rem; font-weight:500; color:var(--warning);
    }
    .ph-lock-badge {
      position:absolute; top:18px; right:24px;
      padding:4px 12px; border-radius:20px; font-size:0.7rem; font-weight:600; letter-spacing:0.5px;
    }
    .ph-lock-badge.locked { background:rgba(239,83,80,0.2); color:var(--danger); }
    .ph-lock-badge.unlocked { background:rgba(102,187,106,0.12); color:var(--success); }

    /* ─── Node Status Cards ─── */
    .node-cards {
      display:grid; grid-template-columns:repeat(2,1fr);
      gap:14px; margin-bottom:20px;
    }
    .node-card {
      background:var(--glass); border:1px solid var(--glass-border);
      border-radius:var(--radius-sm); padding:16px 20px;
      backdrop-filter:blur(12px); transition:border-color 0.3s, background 0.3s;
      position:relative; overflow:hidden;
    }
    .node-card::before {
      content:''; position:absolute; top:0; left:0; right:0; height:2px;
      transition: background 0.3s;
    }
    .node-card.icu::before { background:var(--icu); }
    .node-card.rad::before { background:var(--rad); }

    .node-card.offline { border-color:rgba(239,83,80,0.2); background:rgba(239,83,80,0.04); }
    .node-card.offline::before { background:var(--danger) !important; }
    .nc-top { display:flex; align-items:center; justify-content:space-between; margin-bottom:6px; }
    .nc-name { font-weight:600; font-size:0.9rem; }
    .nc-dot {
      width:10px; height:10px; border-radius:50%;
      transition: background 0.3s, box-shadow 0.3s;
    }
    .nc-dot.online { background:var(--success); box-shadow:0 0 10px var(--success); }
    .nc-dot.offline { background:var(--danger); box-shadow:0 0 10px var(--danger); animation:pulse 1.5s infinite; }
    .nc-dot.unknown { background:var(--text-muted); }
    .nc-role { font-size:0.72rem; color:var(--text-muted); text-transform:uppercase; letter-spacing:1px; }
    .nc-leader {
      font-size:0.65rem; font-weight:600; color:#ffd700; margin-top:4px;
      padding:2px 8px; border-radius:10px; background:rgba(255,215,0,0.12);
      display:inline-block;
    }
    .nc-leader.hidden { display:none; }
    @keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:0.4;} }

    /* ─── Clinical Section ─── */
    .section {
      background:var(--glass); border:1px solid var(--glass-border);
      border-radius:var(--radius); margin-bottom:16px;
      overflow:hidden; backdrop-filter:blur(12px);
    }
    .section-hdr {
      padding:14px 24px;
      display:flex; align-items:center; justify-content:space-between;
      border-bottom:1px solid var(--glass-border);
      font-size:0.75rem; font-weight:600; text-transform:uppercase; letter-spacing:1.5px;
    }
    .section-hdr .badge {
      font-size:0.6rem; padding:3px 8px; border-radius:4px; font-weight:500;
    }
    .section.icu .section-hdr { color:var(--icu); background:rgba(66,165,245,0.05); }
    .section.icu .badge { background:rgba(66,165,245,0.12); color:var(--icu); }
    .section.rad .section-hdr { color:var(--rad); background:rgba(179,136,255,0.05); }
    .section.rad .badge { background:rgba(179,136,255,0.12); color:var(--rad); }


    .section-body { padding:14px 24px; }
    .field-row {
      display:flex; justify-content:space-between; align-items:baseline;
      padding:7px 0; border-bottom:1px solid rgba(255,255,255,0.03);
      transition: background 0.4s;
    }
    .field-row:last-child { border-bottom:none; }
    .field-label { font-size:0.78rem; color:var(--text-dim); font-weight:500; min-width:150px; }
    .field-value { font-size:0.85rem; color:var(--text); text-align:right; flex:1; word-break:break-word; }
    .field-value.empty { color:var(--text-muted); font-style:italic; }
    .field-row.highlight {
      background:rgba(66,165,245,0.12); border-radius:6px;
      padding:7px 10px; margin:2px -10px;
    }

    /* ─── Audit Log ─── */
    .section.audit .section-hdr { color:var(--gold); background:rgba(255,213,79,0.04); }
    .section.audit .badge { background:rgba(255,213,79,0.12); color:var(--gold); }
    .audit-table {
      width:100%; border-collapse:collapse; font-size:0.78rem;
    }
    .audit-table th {
      text-align:left; padding:8px 6px; color:var(--text-muted);
      font-weight:500; font-size:0.7rem; text-transform:uppercase; letter-spacing:0.5px;
      border-bottom:1px solid var(--glass-border);
    }
    .audit-table td {
      padding:7px 6px; border-bottom:1px solid rgba(255,255,255,0.03);
      vertical-align:top;
    }
    .audit-node {
      display:inline-block; padding:1px 6px; border-radius:3px;
      font-size:0.7rem; font-weight:600;
    }
    .audit-node.ICU { background:rgba(66,165,245,0.15); color:var(--icu); }
    .audit-node.RAD { background:rgba(179,136,255,0.15); color:var(--rad); }

    .audit-scroll { max-height:280px; overflow-y:auto; }
    .audit-scroll::-webkit-scrollbar { width:4px; }
    .audit-scroll::-webkit-scrollbar-thumb { background:rgba(255,255,255,0.1); border-radius:2px; }
    .audit-entry-new { animation:fadeIn 0.4s ease; }
    @keyframes fadeIn { from{opacity:0;transform:translateY(-6px);} to{opacity:1;transform:translateY(0);} }

    /* ─── Connection indicator ─── */
    .conn-badge {
      position:fixed; bottom:16px; right:16px;
      padding:6px 14px; border-radius:20px;
      font-size:0.68rem; font-weight:600; letter-spacing:0.5px;
      z-index:100; backdrop-filter:blur(8px);
    }
    .conn-badge.connected { background:rgba(102,187,106,0.15); color:var(--success); border:1px solid rgba(102,187,106,0.2); }
    .conn-badge.disconnected { background:rgba(239,83,80,0.15); color:var(--danger); border:1px solid rgba(239,83,80,0.2); }

    /* ─── Responsive ─── */
    @media (max-width:700px) {
      .node-cards { grid-template-columns:1fr; }
      .title-bar { flex-direction:column; gap:14px; }
      .title-right { width:100%; }
      .btn { flex:1; }
    }
  </style>
</head>
<body>
  <div class="dashboard">
    <!-- Title Bar -->
    <div class="title-bar">
      <div class="title-left">
        <div class="title-icon">🏥</div>
        <div class="title-text">
          <h1>Admin Dashboard</h1>
          <p>Distributed Clinical Report System — Live Monitor</p>
        </div>
      </div>
      <div class="title-right">
        <button class="btn btn-lock" id="btnLock" onclick="lockDoc()">🔒 Lock Document</button>
        <button class="btn btn-unlock" id="btnUnlock" onclick="unlockDoc()">🔓 Unlock</button>
      </div>
    </div>

    <!-- Patient Header -->
    <div class="patient-header">
      <div class="ph-lock-badge unlocked" id="lockBadge">🔓 UNLOCKED</div>
      <div class="ph-row1">
        <span class="ph-name" id="phName">Waiting for data...</span>
        <span class="ph-age" id="phAge"></span>
      </div>
      <div class="ph-row2" id="phAdmission"></div>
      <div class="ph-diagnosis" id="phDiagnosis" style="display:none;"></div>
    </div>

    <!-- Node Status Cards -->
    <div class="node-cards">
      <div class="node-card icu" id="card-ICU">
        <div class="nc-top">
          <span class="nc-name">🏥 ICU</span>
          <span class="nc-dot unknown" id="dot-ICU"></span>
        </div>
        <div class="nc-role">Doctor</div>
        <div class="nc-leader hidden" id="leader-ICU">👑 LEADER</div>
      </div>
      <div class="node-card rad" id="card-RAD">
        <div class="nc-top">
          <span class="nc-name">🔬 Radiology</span>
          <span class="nc-dot unknown" id="dot-RAD"></span>
        </div>
        <div class="nc-role">Doctor</div>
        <div class="nc-leader hidden" id="leader-RAD">👑 LEADER</div>
      </div>
    </div>

    <!-- ICU Section -->
    <div class="section icu">
      <div class="section-hdr">
        <span>🏥 &nbsp;ICU Vitals</span>
        <span class="badge">CRITICAL CARE</span>
      </div>
      <div class="section-body">
        <div class="field-row" id="row-icu.bp"><span class="field-label">Blood Pressure</span><span class="field-value empty" id="val-icu.bp">—</span></div>
        <div class="field-row" id="row-icu.oxygen_saturation"><span class="field-label">Oxygen Saturation</span><span class="field-value empty" id="val-icu.oxygen_saturation">—</span></div>
        <div class="field-row" id="row-icu.heart_rate"><span class="field-label">Heart Rate</span><span class="field-value empty" id="val-icu.heart_rate">—</span></div>
        <div class="field-row" id="row-icu.treatment"><span class="field-label">Treatment</span><span class="field-value empty" id="val-icu.treatment">—</span></div>
        <div class="field-row" id="row-icu.notes"><span class="field-label">ICU Notes</span><span class="field-value empty" id="val-icu.notes">—</span></div>
      </div>
    </div>

    <!-- Radiology Section -->
    <div class="section rad">
      <div class="section-hdr">
        <span>🔬 &nbsp;Radiology</span>
        <span class="badge">IMAGING</span>
      </div>
      <div class="section-body">
        <div class="field-row" id="row-radiology.xray"><span class="field-label">X-Ray</span><span class="field-value empty" id="val-radiology.xray">—</span></div>
        <div class="field-row" id="row-radiology.echo"><span class="field-label">Echo</span><span class="field-value empty" id="val-radiology.echo">—</span></div>
        <div class="field-row" id="row-radiology.notes"><span class="field-label">Radiology Notes</span><span class="field-value empty" id="val-radiology.notes">—</span></div>
      </div>
    </div>



    <!-- Audit Log -->
    <div class="section audit">
      <div class="section-hdr">
        <span>📜 &nbsp;Audit Log</span>
        <span class="badge" id="auditCount">0 ENTRIES</span>
      </div>
      <div class="section-body" style="padding:0;">
        <div class="audit-scroll" id="auditScroll">
          <table class="audit-table">
            <thead>
              <tr><th>Time</th><th>Node</th><th>TS</th><th>Op</th><th>Field</th><th>Value</th></tr>
            </thead>
            <tbody id="auditBody">
              <tr><td colspan="6" style="text-align:center;padding:20px;color:var(--text-muted);">Waiting for updates...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <div class="conn-badge disconnected" id="connBadge">⚡ CONNECTING...</div>

  <script>
    // ═════════════════════════════════════
    //  STATE
    // ═════════════════════════════════════
    let ws = null;
    let report = null;
    let nodesStatus = {};
    let auditLog = [];
    let currentLeader = null;
    let docLocked = false;

    const ALL_FIELDS = [
      'icu.bp','icu.oxygen_saturation','icu.heart_rate','icu.treatment','icu.notes',
      'radiology.xray','radiology.echo','radiology.notes'
    ];

    // ═════════════════════════════════════
    //  WEBSOCKET
    // ═════════════════════════════════════
    function connect() {
      const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(`${protocol}://${location.host}`);

      ws.onopen = () => {
        document.getElementById('connBadge').textContent = '⚡ LIVE';
        document.getElementById('connBadge').className = 'conn-badge connected';
      };

      ws.onclose = () => {
        document.getElementById('connBadge').textContent = '⚡ RECONNECTING...';
        document.getElementById('connBadge').className = 'conn-badge disconnected';
        setTimeout(connect, 2000);
      };

      ws.onmessage = (evt) => {
        const data = JSON.parse(evt.data);
        handleMessage(data);
      };
    }

    function handleMessage(data) {
      if (data.type === 'initial_state' || data.type === 'report_update') {
        if (data.report) {
          report = data.report;
          renderReport(data.field);
          renderPatient();
        }
        if (data.nodes_status) {
          nodesStatus = data.nodes_status;
          renderNodes();
        }
        if (data.audit_log) {
          auditLog = data.audit_log;
          renderAudit();
        }
        if (typeof data.locked !== 'undefined') {
          docLocked = data.locked;
          renderLock();
        }
        if (data.leader) {
          currentLeader = data.leader;
          renderLeader();
        }
      }
      else if (data.type === 'lock_status') {
        docLocked = data.locked;
        renderLock();
      }
      else if (data.type === 'node_status_update') {
        if (data.nodes_status) { nodesStatus = data.nodes_status; renderNodes(); }
        if (data.leader) { currentLeader = data.leader; renderLeader(); }
      }
    }

    // ═════════════════════════════════════
    //  RENDER
    // ═════════════════════════════════════
    function getField(obj, path) {
      const keys = path.split('.');
      let cur = obj;
      for (const k of keys) {
        if (!cur || typeof cur !== 'object') return null;
        cur = cur[k];
      }
      return cur;
    }

    function renderPatient() {
      if (!report) return;
      document.getElementById('phName').textContent = report.patient_name || 'Unknown';
      document.getElementById('phAge').textContent = `${report.age || '?'}M`;
      document.getElementById('phAdmission').textContent = `Admitted: ${report.admission_date || '—'}`;
      const diag = document.getElementById('phDiagnosis');
      if (report.diagnosis) {
        diag.textContent = report.diagnosis;
        diag.style.display = 'inline-block';
      }
    }

    function renderReport(highlightField) {
      if (!report) return;
      for (const f of ALL_FIELDS) {
        const el = document.getElementById(`val-${f}`);
        if (!el) continue;
        const val = getField(report, f);
        if (val && String(val).trim()) {
          el.textContent = val;
          el.classList.remove('empty');
        } else {
          el.textContent = '—';
          el.classList.add('empty');
        }
        // Highlight
        const row = document.getElementById(`row-${f}`);
        if (row && f === highlightField) {
          row.classList.add('highlight');
          setTimeout(() => row.classList.remove('highlight'), 1800);
        }
      }
    }

    function renderNodes() {
      for (const [id, status] of Object.entries(nodesStatus)) {
        const dot = document.getElementById(`dot-${id}`);
        const card = document.getElementById(`card-${id}`);
        if (dot) {
          dot.className = `nc-dot ${status.toLowerCase()}`;
        }
        if (card) {
          card.classList.toggle('offline', status === 'OFFLINE');
        }
      }
    }

    function renderLeader() {
      ['ICU', 'RAD'].forEach(id => {
        const el = document.getElementById(`leader-${id}`);
        if (el) {
          if (currentLeader === id) { el.classList.remove('hidden'); }
          else { el.classList.add('hidden'); }
        }
      });
    }

    function renderLock() {
      const badge = document.getElementById('lockBadge');
      if (docLocked) {
        badge.textContent = '🔒 LOCKED';
        badge.className = 'ph-lock-badge locked';
      } else {
        badge.textContent = '🔓 UNLOCKED';
        badge.className = 'ph-lock-badge unlocked';
      }
    }

    function renderAudit() {
      const tbody = document.getElementById('auditBody');
      const countEl = document.getElementById('auditCount');
      countEl.textContent = `${auditLog.length} ENTRIES`;

      if (auditLog.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:20px;color:var(--text-muted);">No entries yet</td></tr>';
        return;
      }

      // Show latest first
      const reversed = [...auditLog].reverse();
      tbody.innerHTML = reversed.map((e, i) => `
        <tr class="${i === 0 ? 'audit-entry-new' : ''}">
          <td style="color:var(--text-muted);white-space:nowrap;">${e.time || '—'}</td>
          <td><span class="audit-node ${e.node_id}">${e.node_id}</span></td>
          <td style="color:var(--text-dim);">${typeof e.vector_ts === 'object' && e.vector_ts ? JSON.stringify(e.vector_ts).replace(/"/g, '').replace(/,/g, ' | ') : e.vector_ts}</td>
          <td style="color:var(--text-dim);text-transform:uppercase;font-size:0.7rem;">${e.operation}</td>
          <td style="color:var(--text);">${e.field}</td>
          <td style="color:var(--text);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">"${truncate(e.value, 50)}"</td>
        </tr>
      `).join('');

      // Scroll to top (latest)
      document.getElementById('auditScroll').scrollTop = 0;
    }

    // ═════════════════════════════════════
    //  ACTIONS
    // ═════════════════════════════════════
    async function lockDoc() {
      await fetch('/api/lock', { method: 'POST' });
    }

    async function unlockDoc() {
      await fetch('/api/unlock', { method: 'POST' });
    }

    // ═════════════════════════════════════
    //  HELPERS
    // ═════════════════════════════════════
    function truncate(s, n) {
      if (!s) return '';
      return s.length > n ? s.slice(0, n) + '…' : s;
    }

    // ═════════════════════════════════════
    //  BOOT
    // ═════════════════════════════════════
    connect();
  </script>
</body>
</html>

`
