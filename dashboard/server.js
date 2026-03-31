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
        lamport_ts: packet.lamport_ts,
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
