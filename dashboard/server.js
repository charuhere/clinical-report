/**
 * Dashboard Server — Admin Dashboard for Distributed Clinical Report System
 *
 * - Express serves static files from public/ on port 4000
 * - WebSocket server on same HTTP server (upgrade)
 * - UDP listener on port 7000 receives DASHBOARD_UPDATE from all nodes
 * - Broadcasts received data to all WebSocket clients
 * - POST /api/lock and /api/unlock send TCP commands to all nodes
 */

const express = require('express');
const http = require('http');
const WebSocket = require('ws');
const dgram = require('dgram');
const net = require('net');
const path = require('path');

const HTTP_PORT = 4000;
const UDP_PORT = 7000;

// All node TCP ports for lock/unlock commands
const NODES = [
  { id: 'ICU', host: 'localhost', tcpPort: 5001 },
  { id: 'RAD', host: 'localhost', tcpPort: 5002 }
];

// ─── State ───
let latestReport = null;
let nodesStatus = {};
let auditLog = [];
let docLocked = false;

// ═══════════════════════════════════════════════════════
//  EXPRESS + WEBSOCKET
// ═══════════════════════════════════════════════════════
const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

const server = http.createServer(app);
const wss = new WebSocket.Server({ server });

// ─── WebSocket connections ───
wss.on('connection', (ws) => {
  console.log('[WS] Client connected');

  // Send current state immediately
  const initial = {
    type: 'initial_state',
    report: latestReport,
    nodes_status: nodesStatus,
    audit_log: auditLog,
    locked: docLocked
  };
  ws.send(JSON.stringify(initial));

  ws.on('close', () => console.log('[WS] Client disconnected'));
});

function broadcastToWS(data) {
  const msg = JSON.stringify(data);
  wss.clients.forEach((client) => {
    if (client.readyState === WebSocket.OPEN) {
      client.send(msg);
    }
  });
}

// ═══════════════════════════════════════════════════════
//  UDP LISTENER — receives updates from Python nodes
// ═══════════════════════════════════════════════════════
const udpServer = dgram.createSocket('udp4');

udpServer.on('message', (msg) => {
  try {
    const packet = JSON.parse(msg.toString());

    if (packet.type === 'DASHBOARD_UPDATE') {
      latestReport = packet.report;
      nodesStatus = packet.nodes_status || nodesStatus;

      if (packet.audit_log && packet.audit_log.length > 0) {
        auditLog = packet.audit_log;
      }

      // Broadcast to all WebSocket clients
      broadcastToWS({
        type: 'report_update',
        report: packet.report,
        node_id: packet.node_id,
        field: packet.field,
        value: packet.value,
        lamport_ts: packet.lamport_ts,
        operation: packet.operation,
        nodes_status: nodesStatus,
        audit_log: auditLog
      });
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
    locked: docLocked
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


