def load_pending_txns(self):
    """Pick up transactions submitted via the login server (port 5000)."""
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
                continue   # already there

            # Reconstruct Transaction object from dict
            try:
                tx_ins = []
                for inp in tx_dict['tx_ins']:
                    prev_tx  = bytes.fromhex(inp['prev_tx'])
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
                print(f"✅ Loaded pending tx into mempool: {txid[:16]}...")
            except Exception as e:
                print(f"⚠️ Could not load pending tx {txid[:16]}: {e}")

        if loaded > 0:
            # Clear the pending file after loading
            os.remove(pending_file)
            print(f"✅ {loaded} pending transaction(s) moved to mempool")

    except Exception as e:
        print(f"⚠️ Pending tx read error: {e}")
