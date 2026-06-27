import os
import json
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

from Blockchain.client.sentBTC import sendBTC
from Blockchain.Backend.Core.Transaction import Transaction, Tx_In, Tx_Out
from Blockchain.Backend.Core.Script import script as Script

load_dotenv()

def minerApp(utxos, memoryPool, port, address, selected_txs=None, pending_peers=None):
    app = Flask(__name__)
    app.secret_key = os.environ.get('SECRET_KEY', '')

    UTXOS         = utxos
    MemPool       = memoryPool
    MINER_ADDRESS = address

    # ── Wallet page ──
    @app.route('/wallet', methods=["GET", "POST"])
    def wallet():
        msg = ""
        if request.method == "POST":
            FromAddress = request.form.get("fromAddress", "").strip()
            ToAddress   = request.form.get("toAddress",   "").strip()
            Amount      = request.form.get("Amount", type=float)
            SendCoin    = sendBTC(FromAddress, ToAddress, Amount, UTXOS)
            TxObj       = SendCoin.prepareTransaction()
            script_pk   = SendCoin.scriptPubKey(FromAddress)
            verified    = True
            if not TxObj:
                msg = "Insufficient Balance"
            elif isinstance(TxObj, Transaction):
                for index, tx in enumerate(TxObj.tx_ins):
                    if not TxObj.verify_input(index, script_pk):
                        verified = False
                if verified:
                    MemPool[TxObj.TxId] = TxObj
                    msg = "Transaction Added to Memory Pool"
        return render_template("wallet.html", msg=msg, minerAddress=MINER_ADDRESS)

    # ── Balance API ──
    @app.route('/balance')
    def balance():
        from Blockchain.Backend.util.util import decode_Base58
        total = 0
        h160  = decode_Base58(MINER_ADDRESS)
        for txid in dict(UTXOS):
            tx = json.loads(UTXOS[txid])
            for txout in tx['tx_outs']:
                if txout and bytes.fromhex(txout['script_pubKey']['cmds'][2]) == h160:
                    total += txout['amount']
        return jsonify({
            'address':          MINER_ADDRESS,
            'balance':          total / 100000000,
            'balance_satoshis': total
        })

    # ── Receive TX from login server ──
    @app.route('/api/receive_tx', methods=['POST'])
    def receive_tx():
        try:
            tx_dict  = request.get_json(force=True)
            tx_ins   = []
            for inp in tx_dict['tx_ins']:
                prev_tx  = bytes.fromhex(inp['prev_tx'])
                cmds_sig = [bytes.fromhex(c) if isinstance(c, str) else c
                            for c in inp['script_sig']['cmds']]
                tx_ins.append(Tx_In(prev_tx, inp['prev_index'], Script(cmds_sig)))
            tx_outs = []
            for out in tx_dict['tx_outs']:
                cmds_pk = []
                for c in out['script_pubKey']['cmds']:
                    cmds_pk.append(c if isinstance(c, int) else bytes.fromhex(c))
                tx_outs.append(Tx_Out(out['amount'], Script(cmds_pk)))
            tx      = Transaction(tx_dict['version'], tx_ins, tx_outs, tx_dict['locktime'])
            tx.TxId = tx_dict['TxId']
            MemPool[tx.TxId] = tx
            print(f"TX received into mempool: {tx.TxId[:16]}...")
            return jsonify({'ok': True, 'txid': tx.TxId})
        except Exception as e:
            print(f"receive_tx error: {e}")
            return jsonify({'ok': False, 'error': str(e)}), 500

    # ── Wallet stats API ──
    @app.route('/api/wallet_stats')
    def wallet_stats():
        from Blockchain.Backend.util.util import decode_Base58
        from Blockchain.Backend.Core.database.database import BlockChainDB
        total = 0
        count = 0
        h160  = decode_Base58(MINER_ADDRESS)
        for txid in dict(UTXOS):
            try:
                tx = json.loads(UTXOS[txid])
                for out in tx['tx_outs']:
                    if out and 'script_pubKey' in out:
                        cmds = out['script_pubKey'].get('cmds', [])
                        if len(cmds) > 2 and bytes.fromhex(cmds[2]) == h160:
                            total += out['amount']
                            count += 1
            except Exception:
                pass

        blocks_mined = 0
        for block in (BlockChainDB().read() or []):
            txs = block.get('Txs', [])
            if txs:
                outs = txs[0].get('tx_outs', [])
                if outs:
                    cmds = outs[0].get('script_pubKey', {}).get('cmds', [])
                    if len(cmds) > 2:
                        try:
                            if bytes.fromhex(cmds[2]) == h160:
                                blocks_mined += 1
                        except Exception:
                            pass

        locked   = 0
        try:
            pending_file = os.path.join('data', 'pending_txns.json')
            if os.path.exists(pending_file) and os.path.getsize(pending_file) > 0:
                with open(pending_file, 'r') as f:
                    pending = json.load(f)
                from Blockchain.Backend.Core.database.database import BlockChainDB as _CDB
                blocks = _CDB().read() or []
                all_outs = {}
                for block in blocks:
                    for tx in block.get('Txs', []):
                        for idx, out in enumerate(tx.get('tx_outs', [])):
                            if out:
                                all_outs[(tx['TxId'], idx)] = out
                for txid, tx_dict in pending.items():
                    for inp in tx_dict.get('tx_ins', []):
                        prev_tx  = inp.get('prev_tx', '')
                        prev_idx = inp.get('prev_index', -1)
                        if prev_tx and prev_tx != '0' * 64:
                            out = all_outs.get((prev_tx, prev_idx))
                            if out:
                                cmds = out.get('script_pubKey', {}).get('cmds', [])
                                if len(cmds) > 2:
                                    try:
                                        stored = bytes.fromhex(cmds[2]) if isinstance(cmds[2], str) else cmds[2]
                                        if stored == h160:
                                            locked += out['amount']
                                    except Exception:
                                        pass
        except Exception:
            pass

        available = max(0, total - locked)

        return jsonify({
            'balance':       total / 100000000,
            'locked':        locked / 100000000,
            'available':     available / 100000000,
            'balance_sat':   total,
            'utxo_count':    count,
            'blocks_mined':  blocks_mined,
            'mempool_count': len(dict(MemPool)),
            'mining_active': True,
        })

    # ── Mempool txns API ──
    @app.route('/api/mempool_txns')
    def mempool_txns():
        txns = []
        for txid, tx_obj in dict(MemPool).items():
            try:
                d = tx_obj.to_dict()
                txns.append({'txid': txid, 'tx_dict': d})
            except Exception:
                pass
        return jsonify({'txns': txns, 'count': len(txns)})

    @app.route('/api/cancel_tx', methods=['POST'])
    def cancel_tx():
        try:
            data = request.get_json(force=True)
            txid = data.get('txid')
            if txid and txid in MemPool:
                del MemPool[txid]
                return jsonify({'ok': True})
            return jsonify({'ok': False, 'error': 'Tx not found'})
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)}), 500

    @app.route('/api/get_selected_txs', methods=['GET'])
    def get_selected_txs():
        if selected_txs is not None:
            return jsonify({'selected': list(selected_txs)})
        return jsonify({'selected': []})

    @app.route('/api/select_tx', methods=['POST'])
    def select_tx():
        if selected_txs is None:
            return jsonify({'ok': False, 'error': 'Not supported'})
        try:
            data  = request.get_json(force=True)
            txids = data.get('txids', [])
            for txid in txids:
                if txid not in selected_txs:
                    selected_txs.append(txid)
            return jsonify({'ok': True, 'selected': list(selected_txs)})
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)}), 500

    @app.route('/api/deselect_tx', methods=['POST'])
    def deselect_tx():
        if selected_txs is None:
            return jsonify({'ok': False, 'error': 'Not supported'})
        try:
            data = request.get_json(force=True)
            txid = data.get('txid')
            if txid in selected_txs:
                selected_txs.remove(txid)
            return jsonify({'ok': True, 'selected': list(selected_txs)})
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)}), 500

    # ── P2P: add peer ──
    @app.route('/api/p2p_add_peer', methods=['POST'])
    def p2p_add_peer():
        """
        Called by run.py when a new miner starts.
        Pushes peer into shared pending_peers list;
        blockchain.py drains it and connects.
        """
        data = request.get_json(force=True) or {}
        peer = data.get('peer', '').strip()
        if not peer:
            return jsonify({'ok': False, 'error': 'Missing peer'}), 400
        try:
            if pending_peers is not None:
                pending_peers.append(peer)
                print(f"[P2P] Queued new peer: {peer}")
                return jsonify({'ok': True, 'peer': peer})
            else:
                return jsonify({'ok': False, 'error': 'pending_peers not available'}), 503
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)}), 500

    # ── P2P: status info ──
    @app.route('/api/p2p_info')
    def p2p_info():
        """
        Returns live P2P stats by sending a probe to our own P2P server.
        The probe handshake has probe=True so the server doesn't register it as a peer.
        """
        import os as _os
        p2p_port = int(_os.environ.get('P2P_PORT', 6001))
        try:
            import socket as _sock, json as _js, time as _t
            s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            s.settimeout(1)
            s.connect(('127.0.0.1', p2p_port))
            # Probe handshake — server returns peer list and closes
            s.sendall((_js.dumps({'type': 'HANDSHAKE', 'port': p2p_port, 'probe': True}) + '\n').encode())
            s.settimeout(2)
            buf = b''
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if b'\n' in buf:
                    break
            s.close()
            line = buf.split(b'\n')[0]
            msg  = _js.loads(line)
            peers = msg.get('peers', []) if msg.get('type') == 'PEERS' else []
            self_addr = f"127.0.0.1:{p2p_port}"
            peers = [p for p in peers if p != self_addr]
            # Read blocks_received counter
            try:
                br_file = _os.path.join('data', f'p2p_blocks_recv_{p2p_port}.txt')
                blocks_recv = int(open(br_file).read().strip()) if _os.path.exists(br_file) else 0
            except Exception:
                blocks_recv = 0
            return jsonify({'peer_count': len(peers), 'peers': peers, 'blocks_received': blocks_recv})
        except Exception:
            return jsonify({'peer_count': 0, 'peers': [], 'blocks_received': 0})

    print(f"Wallet running on port {port} for {MINER_ADDRESS}")
    app.run(host='0.0.0.0', port=port, debug=False)