import os
from Blockchain.Backend.Core.Script import script
from Blockchain.Backend.util.util import int_to_little_endian, little_endian_to_int, bytes_needed, decode_Base58, encode_varient, hash256
from Blockchain.Backend.Core.database.database import AccountDB

ZERO_HASH = b'\0'*32
REWARD = 3.125

SIGHASH_ALL = 1

def getMinerAccount():
    # ✅ Read from environment variable set by blockchain.py
    minerAddress = os.environ.get('MINER_ADDRESS', None)
    accounts = AccountDB().read()

    if not accounts:
        raise Exception("❌ No account found! Run account.py first")

    if minerAddress:
        for acc in accounts:
            if acc['PublicAddress'] == minerAddress:
                print(f"✅ Mining as: {minerAddress}")
                return acc['PublicAddress'], acc['privateKey']
        raise Exception(f"❌ Address {minerAddress} not found!")

    return accounts[0]['PublicAddress'], accounts[0]['privateKey']

MINER_ADD, PRIVATE_KEY = getMinerAccount()

class CoinBaseTxn:
    def __init__(self, BlockHeight):
        self.BlockHeight = int_to_little_endian(BlockHeight, bytes_needed(BlockHeight))

    def CoinBaseTransaction(self):
        prev_txn = ZERO_HASH
        prev_index = 0xffffffff 

        tns_ins = []
        tns_ins.append(Tx_In(prev_txn, prev_index))
        tns_ins[0].script_sig.cmds.append(self.BlockHeight)

        tns_out = []
        target_amount = int(REWARD * 100000000)
        target_h160 = decode_Base58(MINER_ADD)
        target_script = script.p2pkh_script(target_h160)
        tns_out.append(Tx_Out(amount=target_amount, script_pubKey=target_script))
        coinBaseTx = Transaction(1, tns_ins, tns_out, 0)
        coinBaseTx.TxId = coinBaseTx.id()

        return coinBaseTx

class Transaction:
    def __init__(self, version, tx_ins, tx_outs, locktime):
        self.version = version
        self.tx_ins = tx_ins
        self.tx_outs = tx_outs
        self.locktime = locktime

    def id(self):
        return self.hash().hex()

    def hash(self):
        return hash256(self.serialize())[::-1]

    def serialize(self):
        result = int_to_little_endian(self.version, 4)
        result += encode_varient(len(self.tx_ins))
        
        for i in self.tx_ins:
            result += i.serialize()

        result += encode_varient(len(self.tx_outs))
        
        for j in self.tx_outs:
            result += j.serialize()

        result += int_to_little_endian(self.locktime, 4)

        return result

    def sign_hash(self, input_index, script_pubKey):
        s = int_to_little_endian(self.version, 4)
        s += encode_varient(len(self.tx_ins))

        for i, txIn in enumerate(self.tx_ins):
            if i==input_index:
                s += Tx_In(txIn.prev_tx, txIn.prev_index, script_pubKey).serialize()
            else:
                s += Tx_In(txIn.prev_tx, txIn.prev_index).serialize()

        s += encode_varient(len(self.tx_outs))

        for txOut in self.tx_outs:
            s += txOut.serialize()

        s += int_to_little_endian(self.locktime, 4)
        s += int_to_little_endian(SIGHASH_ALL, 4)

        h256 = hash256(s)
        return int.from_bytes(h256, 'big')


    def sign_input(self, input_index, private_key, script_pubKey):
        z = self.sign_hash(input_index, script_pubKey)
        der = private_key.sign(z).der()
        sig = der + SIGHASH_ALL.to_bytes(1, 'big')
        sec = private_key.point.sec()
        self.tx_ins[input_index].script_sig = script([sig, sec])

    def verify_input(self, input_index, script_pubkey):
        tx_in = self.tx_ins[input_index]
        z = self.sign_hash(input_index, script_pubkey)
        combined = tx_in.script_sig + script_pubkey

        return combined.evaluate(z)

    def is_coinBase(self):
        if len(self.tx_ins) != 1:
            return False
        
        first_input = self.tx_ins[0]
        if first_input.prev_tx != b'\x00' *32:
            return False
        
        if first_input.prev_index != 0xffffffff:
            return False
        
        
        return True

    def to_dict(self):
        import copy
        obj = copy.deepcopy(self)

        for tx_index, tx_in in enumerate(obj.tx_ins):
            if obj.is_coinBase():
                tx_in.script_sig.cmds[0] = little_endian_to_int(tx_in.script_sig.cmds[0])  # ✅ fixed typo

            tx_in.prev_tx = tx_in.prev_tx.hex()

            for index, cmd in enumerate(tx_in.script_sig.cmds):
                if isinstance(cmd, bytes):
                    tx_in.script_sig.cmds[index] = cmd.hex()

            tx_in.script_sig = tx_in.script_sig.__dict__
            obj.tx_ins[tx_index] = tx_in.__dict__

        for index, tx_out in enumerate(obj.tx_outs):
            tx_out.script_pubKey.cmds[2] = tx_out.script_pubKey.cmds[2].hex()
            tx_out.script_pubKey = tx_out.script_pubKey.__dict__
            obj.tx_outs[index] = tx_out.__dict__

        return obj.__dict__


class Tx_In:
    def __init__(self, prev_tx, prev_index, script_sig = None, sequence = 0xffffffff):
        self.prev_tx = prev_tx
        self.prev_index = prev_index

        if script_sig is None:
            self.script_sig = script()
        else:
            self.script_sig = script_sig

        self.sequence = sequence

    def serialize(self):
        result = self.prev_tx[::-1]
        result += int_to_little_endian(self.prev_index, 4)
        result += self.script_sig.serialize()
        result += int_to_little_endian(self.sequence, 4)

        return result

class Tx_Out:
    def __init__(self, amount, script_pubKey):
        self.amount = amount
        self.script_pubKey = script_pubKey

    def serialize(self):
        result = int_to_little_endian(self.amount, 8)
        result += self.script_pubKey.serialize()
        return result