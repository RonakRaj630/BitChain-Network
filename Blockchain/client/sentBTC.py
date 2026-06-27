import json
import copy

from Blockchain.Backend.util.util import decode_Base58
from Blockchain.Backend.Core.Script import script
from Blockchain.Backend.Core.database.database import AccountDB, BlockChainDB
from Blockchain.Backend.Core.Transaction import Tx_In, Tx_Out, Transaction
from Blockchain.Backend.Core.EllepticCurve.EllepticCurve import PrivateKey

COIN        = 100000000
MIN_FEE     = 100000       # 0.001 BTC minimum
MAX_FEE     = 100000000    # 1.000 BTC maximum
FEE_PERCENT = 0.001        # 0.1% of amount


def calculate_fee(amount_satoshis):
    """0.1% of amount, min 0.001 BTC, max 1 BTC"""
    fee = int(amount_satoshis * FEE_PERCENT)
    return max(MIN_FEE, min(fee, MAX_FEE))


def get_spendable_utxos(address):
    """
    Read blockchain ONCE. Return only outputs that:
    1. Belong to `address`
    2. Have NOT been spent in any later transaction
    Returns list of (txid_str, idx, amount) sorted smallest first.
    """
    try:
        h160 = decode_Base58(address)
    except Exception as e:
        print(f"❌ Bad address: {e}")
        return []

    blocks = BlockChainDB().read() or []   # read ONCE

    # Pass 1: collect every output belonging to this address
    candidates = {}   # (txid, idx) -> amount

    for block in blocks:
        for tx in block.get('Txs', []):
            txid = tx.get('TxId', '')
            for idx, out in enumerate(tx.get('tx_outs', [])):
                if out is None:
                    continue
                cmds = out.get('script_pubKey', {}).get('cmds', [])
                if len(cmds) > 2:
                    try:
                        stored = bytes.fromhex(cmds[2]) if isinstance(cmds[2], str) else cmds[2]
                        if stored == h160:
                            candidates[(txid, idx)] = out['amount']
                    except Exception:
                        pass

    # Pass 2: remove every output that appears as an input (confirmed spent)
    for block in blocks:
        for tx in block.get('Txs', []):
            for inp in tx.get('tx_ins', []):
                spent_id  = inp.get('prev_tx', '')
                spent_idx = inp.get('prev_index', -1)
                if spent_id and spent_id != '0' * 64:
                    candidates.pop((spent_id, spent_idx), None)

    # Pass 3: also remove UTXOs that are in pending_txns.json (unconfirmed mempool)
    # This prevents double-spending while a transaction is awaiting confirmation
    try:
        import os, json as _json
        pending_file = os.path.join('data', 'pending_txns.json')
        if os.path.exists(pending_file) and os.path.getsize(pending_file) > 0:
            with open(pending_file, 'r') as f:
                pending = _json.load(f)
            for txid, tx_dict in pending.items():
                for inp in tx_dict.get('tx_ins', []):
                    spent_id  = inp.get('prev_tx', '')
                    spent_idx = inp.get('prev_index', -1)
                    if spent_id and spent_id != '0' * 64:
                        candidates.pop((spent_id, spent_idx), None)
    except Exception:
        pass

    # Sort smallest first — minimises number of inputs needed
    result = [(txid, idx, amount)
              for (txid, idx), amount in candidates.items()]
    result.sort(key=lambda x: x[2])
    return result


class sendBTC:
    def __init__(self, fromAcc, toAcc, Amount, UTXOS, fee_amount=0):
        self.FromPublicAdd   = fromAcc
        self.toAcc           = toAcc
        self.Amount          = int(round(Amount * COIN))
        self.fee             = int(round(fee_amount * COIN))  # ← manual fee
        self.Total           = 0
        self.isBalanceEnough = False
        # UTXOS param kept for API compatibility but we always use DB

        print(f"💰 Send {self.Amount/COIN:.4f} BTC | Fee {self.fee/COIN:.6f} BTC ({self.fee/self.Amount*100:.2f}%)")

    def scriptPubKey(self, addr):
        return script().p2pkh_script(decode_Base58(addr))

    def getPrivateKey(self):
        acc = AccountDB().getAccountByAddress(self.FromPublicAdd)
        return acc['privateKey'] if acc else None

    def prepareTxIn(self):
        self.Total = 0
        TxIns      = []
        self.From_Address_Script_PubKey = self.scriptPubKey(self.FromPublicAdd)
        self.fromPubkeyHash = self.From_Address_Script_PubKey.cmds[2]

        need      = self.Amount + self.fee
        spendable = get_spendable_utxos(self.FromPublicAdd)

        if not spendable:
            print(f"❌ No spendable UTXOs for {self.FromPublicAdd}")
            self.isBalanceEnough = False
            return []

        # Pick UTXOs until we have enough
        for txid, idx, amount in spendable:
            if self.Total >= need:
                break
            self.Total += amount
            TxIns.append(Tx_In(bytes.fromhex(txid), idx))

        self.isBalanceEnough = self.Total >= need

        if not self.isBalanceEnough:
            print(f"❌ Need {need/COIN:.8f} BTC, only have {self.Total/COIN:.8f} BTC")
        else:
            print(f"✅ {len(TxIns)} UTXOs collected, total {self.Total/COIN:.8f} BTC")

        return TxIns

    def prepareTxOut(self):
        TxOuts = []
        TxOuts.append(Tx_Out(self.Amount, self.scriptPubKey(self.toAcc)))

        change = self.Total - self.Amount - self.fee
        if change < 0:
            self.fee = max(0, self.Total - self.Amount)
            change   = 0
            print(f"⚠️ Adjusted fee to {self.fee/COIN:.6f} BTC, change=0")

        if change > 0:
            TxOuts.append(Tx_Out(change, self.From_Address_Script_PubKey))

        print(f"✅ send={self.Amount/COIN:.4f} | fee={self.fee/COIN:.6f} | change={change/COIN:.4f}")
        return TxOuts

    def signTx(self):
        secret = self.getPrivateKey()
        if not secret:
            print(f"❌ No private key for {self.FromPublicAdd}")
            return False
        priv = PrivateKey(secret=int(secret))
        for i in range(len(self.TxIns)):
            self.TxObj.sign_input(i, priv, self.From_Address_Script_PubKey)
        return True

    def prepareTransaction(self):
        self.TxIns = self.prepareTxIn()
        if not self.isBalanceEnough:
            return False
        self.TxOuts = self.prepareTxOut()
        self.TxObj  = Transaction(1, self.TxIns, self.TxOuts, 0)
        self.TxObj.TxId = self.TxObj.id()
        if not self.signTx():
            return False
        return self.TxObj
