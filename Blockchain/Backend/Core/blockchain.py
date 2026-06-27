import sys
import os
import argparse
import json
import threading
import time

# Parse arguments FIRST
parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int, default=5001)
parser.add_argument('--address', type=str, required=True)
args = parser.parse_args()

os.environ['MINER_ADDRESS'] = args.address

import pathlib
ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from Blockchain.Backend.Core.block import Block
from Blockchain.Backend.Core.blockheader import BlockHeader
from Blockchain.Backend.util.util import hash256, merkle_root, target_to_bits
from Blockchain.Backend.Core.database.database import BlockChainDB
from Blockchain.Backend.Core.Transaction import CoinBaseTxn
from Blockchain.Backend.Core.database.database import AccountDB
from multiprocessing import Process, Manager
from Blockchain.run import main

from Blockchain.Backend.Core.p2p import P2PNode

ZERO_HASH = '0' * 64
VERSION = 1.1
INITIAL_TARGET = 0x0000ffff00000000000000000000000000000000000000000000000000000000


class BlockChain:
    def __init__(self, utxos, memoryPool, selected_txs=None):
        self.utxos = utxos
        self.memoryPool = memoryPool
        self.selected_txs = selected_txs
        self.current_target = INITIAL_TARGET
        self.bits = target_to_bits(INITIAL_TARGET)
        self.p2p_node = None
        self.mining_interrupted = False

    def write_on_disk(self, block):
        blockChainDB = BlockChainDB()
        return blockChainDB.write(block)   # returns True=won, False=lost

    def fetch_last_block(self):
        blockChainDB = BlockChainDB()
        return blockChainDB.lastBlock()

    def store_utxos_in_cache(self):
        for tx in self.TxJSON:
            self.utxos[tx['TxId']] = json.dumps(tx)
            print(f"UTXO stored: {tx['TxId']}")
            print(f"cmds[2] stored as: {tx['tx_outs'][0]['script_pubKey']['cmds'][2]}")

    def remove_spent_transaction(self):
        for txId_index in self.spentTxs:
            txId_hex = txId_index[0].hex()
            if txId_hex in self.utxos:
                tx = json.loads(self.utxos[txId_hex])
                if len(tx['tx_outs']) < 2:
                    print(f"Spent Transaction Removed: {txId_hex}")
                    del self.utxos[txId_hex]
                else:
                    tx['tx_outs'].pop(txId_index[1])
                    self.utxos[txId_hex] = json.dumps(tx)
                    print(f"Spent output removed from: {txId_hex}")

    def read_transaction_from_memorypool(self):
        from Blockchain.Backend.util.util import decode_Base58
        miner_addr = os.environ.get('MINER_ADDRESS')
        try:
            miner_h160 = decode_Base58(miner_addr) if miner_addr else None
        except Exception:
            miner_h160 = None

        self.Blocksize = 80
        self.TxIds = []
        self.addTransactionInBlock = []
        self.spentTxs = []

        MAX_TX_PER_BLOCK = 5
        candidates = []
        invalid_txs = []

        for tx in self.memoryPool:
            tx_obj = self.memoryPool[tx]
            is_own_tx = False
            if miner_h160:
                for out in tx_obj.tx_outs:
                    cmds = out.script_pubKey.cmds
                    val = bytes.fromhex(cmds[2]) if isinstance(cmds[2], str) else cmds[2]
                    if len(cmds) > 2 and val == miner_h160:
                        is_own_tx = True
                        break
                if not is_own_tx:
                    for inp in tx_obj.tx_ins:
                        prev_tx_hex = inp.prev_tx.hex()
                        if prev_tx_hex in self.utxos:
                            try:
                                prev_tx = json.loads(self.utxos[prev_tx_hex])
                                outs = prev_tx.get('tx_outs', [])
                                idx = inp.prev_index
                                if idx < len(outs) and outs[idx]:
                                    cmds = outs[idx].get('script_pubKey', {}).get('cmds', [])
                                    if len(cmds) > 2:
                                        stored = bytes.fromhex(cmds[2]) if isinstance(cmds[2], str) else cmds[2]
                                        if stored == miner_h160:
                                            is_own_tx = True
                                            break
                            except Exception:
                                pass

            if is_own_tx:
                print(f"Skipping TX {tx[:16]}... (Miner is sender/receiver)")
                continue

            if self.selected_txs is not None and tx not in self.selected_txs:
                continue

            input_amount = 0
            is_valid = True
            for inp in tx_obj.tx_ins:
                prev_tx_hex = inp.prev_tx.hex()
                if prev_tx_hex in self.utxos:
                    try:
                        prev_tx = json.loads(self.utxos[prev_tx_hex])
                        outs = prev_tx.get('tx_outs', [])
                        idx = inp.prev_index
                        if idx < len(outs) and outs[idx]:
                            input_amount += outs[idx]['amount']
                        else:
                            is_valid = False
                    except Exception:
                        is_valid = False
                else:
                    is_valid = False

            if not is_valid:
                print(f"Dropping invalid TX {tx[:16]}...")
                invalid_txs.append(tx)
                continue

            output_amount = sum([o.amount for o in tx_obj.tx_outs])
            fee = max(0, input_amount - output_amount)
            candidates.append((fee, tx, tx_obj))

        for tx in invalid_txs:
            if tx in self.memoryPool:
                del self.memoryPool[tx]
            if self.selected_txs is not None and tx in self.selected_txs:
                self.selected_txs.remove(tx)

        candidates.sort(key=lambda x: x[0], reverse=True)
        selected = candidates[:MAX_TX_PER_BLOCK]

        for fee, tx, tx_obj in selected:
            self.TxIds.append(bytes.fromhex(tx))
            self.addTransactionInBlock.append(tx_obj)
            self.Blocksize += len(tx_obj.serialize())
            for spent in tx_obj.tx_ins:
                self.spentTxs.append([spent.prev_tx, spent.prev_index])
            if self.selected_txs is not None and tx in self.selected_txs:
                self.selected_txs.remove(tx)

    def remove_transactions_from_memorypool(self):
        for tx in self.TxIds:
            if tx.hex() in self.memoryPool:
                del self.memoryPool[tx.hex()]
        try:
            pending_file = os.path.join('data', 'pending_txns.json')
            if os.path.exists(pending_file):
                with open(pending_file, 'r') as f:
                    pending = json.load(f)
                changed = False
                for tx in self.TxIds:
                    tx_hex = tx.hex()
                    if tx_hex in pending:
                        del pending[tx_hex]
                        changed = True
                if changed:
                    with open(pending_file, 'w') as f:
                        json.dump(pending, f, indent=4)
        except Exception as e:
            print(f"Error removing from pending file: {e}")

    def GenesisBlock(self):
        BlockHeight = 0
        prevBlockHash = ZERO_HASH
        self.addBlock(BlockHeight, prevBlockHash)

    def convert_to_Json(self):
        self.TxJSON = []
        for tx in self.addTransactionInBlock:
            if isinstance(tx, dict):
                self.TxJSON.append(tx)
            else:
                self.TxJSON.append(tx.to_dict())

    def calculate_fee(self):
        self.input_amount = 0
        self.output_amount = 0
        for TxID_index in self.spentTxs:
            txId_hex = TxID_index[0].hex()
            if txId_hex in self.utxos:
                tx = json.loads(self.utxos[txId_hex])
                self.input_amount += tx['tx_outs'][TxID_index[1]]['amount']
        for TX in self.addTransactionInBlock:
            if isinstance(TX, dict):
                for tx_out in TX['tx_outs']:
                    self.output_amount += tx_out['amount']
            else:
                for tx_out in TX.tx_outs:
                    self.output_amount += tx_out.amount
        self.fee = max(0, self.input_amount - self.output_amount)
        print(f"Fee calculated: {self.fee}")

    def load_pending_txns(self):
        import json, os
        from Blockchain.Backend.Core.Transaction import Transaction, Tx_In, Tx_Out
        from Blockchain.Backend.Core.Script import script

        pending_file = os.path.join('data', 'pending_txns.json')
        if not os.path.exists(pending_file):
            return
        try:
            with open(pending_file, 'r') as f:
                pending = json.load(f)
            if not pending:
                return
            loaded = 0
            for txid, tx_dict in list(pending.items()):
                if txid in self.memoryPool:
                    continue
                try:
                    tx_ins = []
                    for inp in tx_dict['tx_ins']:
                        prev_tx = bytes.fromhex(inp['prev_tx'])
                        cmds_sig = [bytes.fromhex(c) if isinstance(c, str) else c
                                    for c in inp['script_sig']['cmds']]
                        tx_ins.append(Tx_In(prev_tx, inp['prev_index'], script(cmds_sig)))
                    tx_outs = []
                    for out in tx_dict['tx_outs']:
                        cmds_pk = []
                        for c in out['script_pubKey']['cmds']:
                            if isinstance(c, int):
                                cmds_pk.append(c)
                            else:
                                cmds_pk.append(bytes.fromhex(c))
                        tx_outs.append(Tx_Out(out['amount'], script(cmds_pk)))
                    tx = Transaction(tx_dict['version'], tx_ins, tx_outs, tx_dict['locktime'])
                    tx.TxId = txid
                    self.memoryPool[txid] = tx
                    loaded += 1
                    print(f"Pending tx loaded: {txid[:16]}...")
                except Exception as e:
                    print(f"Could not load tx {txid[:16]}: {e}")
            if loaded > 0:
                print(f"{loaded} pending tx(s) loaded into mempool")
        except Exception as e:
            print(f"Pending tx read error: {e}")

    def addBlock(self, BlockHeight, prevBlockHash):
        self.mining_interrupted = False

        self.read_transaction_from_memorypool()
        self.load_pending_txns()
        self.calculate_fee()

        timestamp = int(time.time())
        CoinBaseInstance = CoinBaseTxn(BlockHeight)
        coinBaseTx = CoinBaseInstance.CoinBaseTransaction()
        self.Blocksize += len(coinBaseTx.serialize())
        coinBaseTx.tx_outs[0].amount = coinBaseTx.tx_outs[0].amount + max(0, self.fee)
        coinBaseTx = coinBaseTx.to_dict()
        self.TxIds.insert(0, bytes.fromhex(coinBaseTx['TxId']))
        self.addTransactionInBlock.insert(0, coinBaseTx)

        merkleRoot = merkle_root(self.TxIds)[::-1].hex()
        blockheader = BlockHeader(VERSION, prevBlockHash, merkleRoot, timestamp, self.bits)

        # Mine with interrupt support
        blockheader.mine(self.current_target, interrupt_flag=self)

        # Interrupted by peer winning
        if self.mining_interrupted:
            print(f"[COMPETE] Peer won Block #{BlockHeight} — moving on")
            self.mining_interrupted = False
            return False

        self.remove_spent_transaction()
        self.remove_transactions_from_memorypool()
        self.convert_to_Json()

        mined_block = Block(BlockHeight, self.Blocksize, blockheader.__dict__, len(self.TxJSON), self.TxJSON).__dict__

        # Try to write — only winner stores UTXOs and broadcasts
        won = self.write_on_disk([mined_block])

        if won is False:
            print(f"[COMPETE] Lost Block #{BlockHeight} — another miner wrote it first")
            return False

        # WON — store UTXOs and broadcast
        self.store_utxos_in_cache()
        print(f"[COMPETE] WON Block #{BlockHeight}! Nonce={blockheader.nonce}")
        if self.p2p_node:
            self.p2p_node.broadcast_block(mined_block)
        return True

    def main(self, pending_peers=None):
        # Start P2P node
        blockChainDB = BlockChainDB()
        self.p2p_node = P2PNode(self, self.memoryPool, blockChainDB)

        initial_peers = [p.strip() for p in os.environ.get('P2P_PEERS', '').split(',') if p.strip()]
        self.p2p_node.start(initial_peers=initial_peers)

        # Peer watcher thread
        if pending_peers is not None:
            _known_connections = set()

            def _peer_watcher():
                nonlocal _known_connections
                while True:
                    try:
                        if len(pending_peers) > 0:
                            addr = pending_peers.pop(0)
                            self.p2p_node.add_peer(addr)
                            print(f"[P2P] Auto-connected to new peer: {addr}")
                        current = set(self.p2p_node.connections.keys())
                        newly = current - _known_connections
                        if newly and _known_connections:
                            print(f"[P2P] Peer(s) reconnected: {newly} — syncing chain")
                            self.p2p_node.sync_chain()
                        _known_connections = current
                    except Exception:
                        pass
                    time.sleep(1)

            threading.Thread(target=_peer_watcher, daemon=True).start()

        # Wait for peers before mining
        if initial_peers:
            print(f"[P2P] Waiting for peers to connect (max 30s)...")
            connected = False
            for _ in range(30):
                time.sleep(1)
                if len(self.p2p_node.connections) > 0:
                    connected = True
                    break
            if connected:
                print(f"[P2P] Peer connected — syncing chain before mining...")
                self.p2p_node.sync_chain()
                time.sleep(3)
            else:
                print(f"[P2P] No peers after 30s — continuing solo")
        else:
            print(f"[P2P] No peers configured — solo mode")
            time.sleep(1)

        lastBlock = self.fetch_last_block()
        if lastBlock is None:
            self.GenesisBlock()

        while True:
            lastBlock = self.fetch_last_block()
            BlockHeight = lastBlock['Height'] + 1
            prevBlockHash = lastBlock['BlockHeader']['blockHash']

            result = self.addBlock(BlockHeight, prevBlockHash)

            if result is False:
                # Lost the race — wait then re-read for next block
                time.sleep(0.3)


def runWallet(utxos, memoryPool, port, address, selected_txs=None, pending_peers=None):
    """Run Flask wallet for this specific miner"""
    from Blockchain.Frontend.minerWallet import minerApp
    os.environ['MINER_ADDRESS'] = address
    minerApp(utxos, memoryPool, port, address, selected_txs, pending_peers)


if __name__ == "__main__":
    print(f"Starting miner: {args.address} on port: {args.port}")

    with Manager() as manager:
        utxos = manager.dict()
        memoryPool = manager.dict()
        selected_txs = manager.list()
        pending_peers = manager.list()

        try:
            from Blockchain.run import build_utxo_set_from_db
            utxos.update(build_utxo_set_from_db())
            print(f"Loaded {len(utxos)} UTXOs from database")
        except Exception as e:
            print(f"Could not load UTXOs from db: {e}")

        webapp = Process(
            target=runWallet,
            args=(utxos, memoryPool, args.port, args.address, selected_txs, pending_peers)
        )
        webapp.start()

        blockchain = BlockChain(utxos, memoryPool, selected_txs)
        blockchain.main(pending_peers)