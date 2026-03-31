/**
 * Dashboard Server — Admin Dashboard for Distributed Clinical Report System
 *
 * Features:
 * - Express serves static files from public/ on port 4000
 * - WebSocket server for real-time browser updates
 * - UDP listener on port 7000 receives DASHBOARD_UPDATE + HEARTBEAT from nodes
 * - Tracks leader election status, checkpoint events, rollback events
 */

const express = require('express');
const http = require('http');
const WebSocket = require('ws');
const dgram = require('dgram');
const path = require('path');

const HTTP_PORT = 4000;
const UDP_PORT = 7000;
const HEARTBEAT_TIMEOUT = 9000; // ms

// Only IDs needed to track heartbeats (Dashboard no longer pushes TCP)
const NODE_IDS = ['ICU', 'RAD'];

// ─── State ───
let latestReport = null;
let nodesStatus = {};
let auditLog = [];
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
  ws.send(JSON.stringify({
    type: 'initial_state',
    report: latestReport,
    nodes_status: nodesStatus,
    audit_log: auditLog,
    leader: currentLeader
  }));
  ws.on('close', () => console.log('[WS] Client disconnected'));
});

function broadcastToWS(data) {
  const msg = JSON.stringify(data);
  for (const client of wss.clients) {
    if (client.readyState === WebSocket.OPEN) client.send(msg);
  }
}

// ═══════════════════════════════════════════════════════
//  UDP LISTENER — receives updates + heartbeats from nodes
// ═══════════════════════════════════════════════════════
const udpServer = dgram.createSocket('udp4');

udpServer.on('message', (msg) => {
  try {
    const packet = JSON.parse(msg.toString());
    const { type, leader, node_id } = packet;

    // Both update types can include leader information
    if (leader) currentLeader = leader;

    if (type === 'DASHBOARD_UPDATE') {
      const { report, nodes_status, audit_log, field, value, vector_ts, operation } = packet;

      latestReport = report;
      nodesStatus = nodes_status || nodesStatus;
      if (audit_log?.length) auditLog = audit_log; // Optional chaining

      broadcastToWS({
        type: 'report_update',
        report, node_id, field, value, vector_ts, operation, // Object shorthand
        nodes_status: nodesStatus, 
        audit_log: auditLog, 
        leader: currentLeader
      });
    } else if (type === 'HEARTBEAT') {
      lastHeartbeat[node_id] = Date.now();

      // Mark as online if previously disconnected
      if (nodesStatus[node_id] !== 'ONLINE') {
        nodesStatus[node_id] = 'ONLINE';
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
  for (const nodeId of NODE_IDS) {
    const lastSeen = lastHeartbeat[nodeId];
    if (lastSeen && (now - lastSeen) > HEARTBEAT_TIMEOUT) {
      if (nodesStatus[nodeId] !== 'OFFLINE') {
        nodesStatus[nodeId] = 'OFFLINE';
        changed = true;
        console.log(`[HEARTBEAT] ${nodeId} is OFFLINE`);
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



// ─── Status endpoint ───
app.get('/api/status', (req, res) => res.json({
  report: latestReport,
  nodes_status: nodesStatus,
  audit_log: auditLog,
  leader: currentLeader
}));

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
