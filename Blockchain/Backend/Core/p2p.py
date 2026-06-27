"""
p2p.py — Peer-to-Peer networking layer for BitChain
Each node runs a TCP server on P2P_PORT (default 6001).
Peers are injected at runtime by run.py — no peers.json needed.
Messages are newline-delimited JSON.
"""

import socket
import threading
import json
import os
import time

P2P_PORT     = int(os.environ.get('P2P_PORT', 6001))
MAX_PEERS    = 20
RECV_TIMEOUT = 60   # seconds


def _send(sock, msg: dict):
    try:
        data = json.dumps(msg) + '\n'
        sock.sendall(data.encode())
    except Exception:
        pass


def _recv(sock) -> dict | None:
    buf = b''
    sock.settimeout(RECV_TIMEOUT)
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            buf += chunk
            if b'\n' in buf:
                line, _ = buf.split(b'\n', 1)
                return json.loads(line.decode())
    except Exception:
        return None


class P2PNode:
    def __init__(self, blockchain_ref, mempool_ref, db_ref):
        self.blockchain  = blockchain_ref
        self.mempool     = mempool_ref
        self.db          = db_ref
        self.peers       = set()
        self.connections = {}   # addr -> socket
        self.lock        = threading.Lock()
        self.running     = True
        self.blocks_received_from_peers = 0

    def start(self, initial_peers=None):
        threading.Thread(target=self._server_loop, daemon=True).start()
        if initial_peers:
            for addr in initial_peers:
                self.peers.add(addr)
                threading.Thread(target=self._connect_to, args=(addr,), daemon=True).start()
        print(f"[P2P] Node listening on port {P2P_PORT} | auto-peers: {initial_peers or []}")

    def broadcast_block(self, block: dict):
        msg = {"type": "NEW_BLOCK", "block": block}
        self._broadcast(msg)
        print(f"[P2P] Broadcast new block #{block.get('Height')} to {len(self.connections)} peers")

    def broadcast_tx(self, tx_dict: dict):
        self._broadcast({"type": "NEW_TX", "tx": tx_dict})

    def add_peer(self, addr: str):
        if addr not in self.peers:
            self.peers.add(addr)
            threading.Thread(target=self._connect_to, args=(addr,), daemon=True).start()

    def sync_chain(self):
        print(f"[P2P] Requesting chain from all peers...")
        self._broadcast({"type": "GET_CHAIN"})

    # ── TCP Server ──────────────────────────────────────────

    def _server_loop(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(('0.0.0.0', P2P_PORT))
        except OSError as e:
            print(f"[P2P] Cannot bind port {P2P_PORT}: {e}")
            return
        srv.listen(MAX_PEERS)
        while self.running:
            try:
                conn, addr = srv.accept()
                threading.Thread(
                    target=self._handle_peer,
                    args=(conn, addr[0]),
                    daemon=True
                ).start()
            except Exception:
                pass

    def _handle_peer(self, conn, ip):
        msg = _recv(conn)
        if not msg:
            conn.close()
            return

        # PROBE: minerWallet status check — respond and close, don't register
        if msg.get('type') == 'HANDSHAKE' and msg.get('probe'):
            _send(conn, {"type": "PEERS", "peers": list(self.peers)})
            conn.close()
            return

        if msg.get('type') != 'HANDSHAKE':
            conn.close()
            return

        peer_port = msg.get('port', P2P_PORT)
        addr = f"{ip}:{peer_port}"

        with self.lock:
            self.peers.add(addr)
            self.connections[addr] = conn

        print(f"[P2P] Connected peer: {addr}")

        # Share peer list and request their chain
        _send(conn, {"type": "PEERS", "peers": list(self.peers)})
        _send(conn, {"type": "GET_CHAIN"})

        # Persistent message loop
        while self.running:
            msg = _recv(conn)
            if msg is None:
                # Timeout — send ping
                try:
                    _send(conn, {"type": "PING"})
                    pong = _recv(conn)
                    if pong and pong.get('type') == 'PONG':
                        continue
                except Exception:
                    pass
                break
            self._handle_message(msg, conn, addr)

        with self.lock:
            self.connections.pop(addr, None)
        conn.close()
        print(f"[P2P] Peer disconnected: {addr}")

    # ── Outbound connections with auto-reconnect ────────────

    def _connect_to(self, addr: str):
        while self.running:
            if addr in self.connections:
                time.sleep(5)
                continue
            try:
                ip, port = addr.rsplit(':', 1)
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect((ip, int(port)))

                _send(sock, {"type": "HANDSHAKE", "port": P2P_PORT})

                with self.lock:
                    self.connections[addr] = sock

                print(f"[P2P] Connected to peer: {addr}")

                while self.running:
                    msg = _recv(sock)
                    if msg is None:
                        try:
                            _send(sock, {"type": "PING"})
                            pong = _recv(sock)
                            if pong and pong.get('type') == 'PONG':
                                continue
                        except Exception:
                            pass
                        break
                    self._handle_message(msg, sock, addr)

            except Exception:
                pass
            finally:
                with self.lock:
                    self.connections.pop(addr, None)

            if self.running:
                time.sleep(5)

    # ── Message handling ────────────────────────────────────

    def _handle_message(self, msg: dict, conn, sender_addr: str):
        mtype = msg.get('type')

        if mtype == 'GET_CHAIN':
            blocks = self.db.read() or []
            _send(conn, {"type": "CHAIN", "blocks": blocks})

        elif mtype == 'CHAIN':
            self._maybe_replace_chain(msg.get('blocks', []))

        elif mtype == 'NEW_BLOCK':
            block = msg.get('block')
            if block:
                self._handle_new_block(block)

        elif mtype == 'NEW_TX':
            tx_dict = msg.get('tx')
            if tx_dict:
                self._handle_new_tx(tx_dict)

        elif mtype == 'GET_PEERS':
            _send(conn, {"type": "PEERS", "peers": list(self.peers)})

        elif mtype == 'PEERS':
            for addr in msg.get('peers', []):
                if addr not in self.peers:
                    self.peers.add(addr)
                    threading.Thread(
                        target=self._connect_to, args=(addr,), daemon=True
                    ).start()

        elif mtype == 'PING':
            _send(conn, {"type": "PONG"})

        elif mtype == 'PONG':
            pass

        elif mtype == 'HANDSHAKE':
            pass

    # ── Chain replacement ───────────────────────────────────

    def _maybe_replace_chain(self, incoming: list):
        if not incoming:
            return
        local = self.db.read() or []
        if len(incoming) <= len(local):
            return
        if not self._is_chain_valid(incoming):
            print(f"[P2P] Received invalid chain — rejected")
            return
        print(f"[P2P] Replacing chain: local={len(local)} peer={len(incoming)} blocks")
        import tempfile
        try:
            serialized = json.dumps(incoming, indent=4)
            fd, tmp = tempfile.mkstemp(dir='data', suffix='.tmp')
            with os.fdopen(fd, 'w') as f:
                f.write(serialized)
            os.replace(tmp, self.db.filepath)
            print(f"[P2P] Chain replaced successfully")
        except Exception as e:
            print(f"[P2P] Chain replace failed: {e}")

    def _is_chain_valid(self, chain: list) -> bool:
        for i in range(1, len(chain)):
            prev = chain[i - 1]
            curr = chain[i]
            if curr.get('Height') != prev.get('Height', -1) + 1:
                return False
            if curr['BlockHeader'].get('prevBlockHash') != prev['BlockHeader'].get('blockHash'):
                return False
        return True

    # ── New block from peer ─────────────────────────────────

    def _handle_new_block(self, block: dict):
        local = self.db.read() or []
        last = local[-1] if local else None

        if last and block.get('Height', -1) <= last.get('Height', -1):
            return

        if last and block['BlockHeader'].get('prevBlockHash') != last['BlockHeader'].get('blockHash'):
            print(f"[P2P] Block doesn't link — requesting full chain")
            self.sync_chain()
            return

        self.db.write([block])
        self.blocks_received_from_peers += 1

        # Write counter for UI
        try:
            br_file = os.path.join('data', f'p2p_blocks_recv_{P2P_PORT}.txt')
            with open(br_file, 'w') as f:
                f.write(str(self.blocks_received_from_peers))
        except Exception:
            pass

        print(f"[P2P] Peer won Block #{block.get('Height')} — stopping our mining")

        # Interrupt our mining immediately
        if self.blockchain and hasattr(self.blockchain, 'mining_interrupted'):
            self.blockchain.mining_interrupted = True

        self._broadcast({"type": "NEW_BLOCK", "block": block})

    # ── New tx from peer ────────────────────────────────────

    def _handle_new_tx(self, tx_dict: dict):
        from Blockchain.Backend.Core.Transaction import Transaction, Tx_In, Tx_Out
        from Blockchain.Backend.Core.Script import script as Script

        txid = tx_dict.get('TxId', '')
        if not txid or txid in self.mempool:
            return
        try:
            tx_ins = []
            for inp in tx_dict['tx_ins']:
                prev_tx = bytes.fromhex(inp['prev_tx'])
                cmds_sig = [bytes.fromhex(c) if isinstance(c, str) else c
                            for c in inp['script_sig']['cmds']]
                tx_ins.append(Tx_In(prev_tx, inp['prev_index'], Script(cmds_sig)))
            tx_outs = []
            for out in tx_dict['tx_outs']:
                cmds_pk = []
                for c in out['script_pubKey']['cmds']:
                    cmds_pk.append(c if isinstance(c, int) else bytes.fromhex(c))
                tx_outs.append(Tx_Out(out['amount'], Script(cmds_pk)))
            tx = Transaction(tx_dict['version'], tx_ins, tx_outs, tx_dict['locktime'])
            tx.TxId = txid
            self.mempool[txid] = tx
            self._broadcast({"type": "NEW_TX", "tx": tx_dict})
        except Exception as e:
            print(f"[P2P] Could not deserialise peer tx: {e}")

    # ── Broadcast ───────────────────────────────────────────

    def _broadcast(self, msg: dict):
        dead = []
        with self.lock:
            snapshot = dict(self.connections)
        for addr, sock in snapshot.items():
            try:
                _send(sock, msg)
            except Exception:
                dead.append(addr)
        with self.lock:
            for addr in dead:
                self.connections.pop(addr, None)