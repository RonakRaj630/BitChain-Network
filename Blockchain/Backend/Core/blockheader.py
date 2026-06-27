from Blockchain.Backend.util.util import hash256, little_endian_to_int, int_to_little_endian

class BlockHeader:
    def __init__(self, version, prevBlockHash, merkleRoot, timestamp, bits):
        self.version = version
        self.prevBlockHash = prevBlockHash
        self.merkleRoot = merkleRoot
        self.timestamp = timestamp
        self.bits = bits
        self.nonce = 0
        self.blockHash = ''

    def mine(self, target, interrupt_flag=None):
        """
        Mine until hash < target.
        interrupt_flag: BlockChain instance — if mining_interrupted becomes True, stop.
        Checks every 500 nonces for fast response.
        """
        self.blockHash = target + 1

        while self.blockHash > target:
            header_string = little_endian_to_int(
                hash256(
                    int_to_little_endian(self.version, 4) +
                    bytes.fromhex(self.prevBlockHash)[::-1] +
                    bytes.fromhex(self.merkleRoot) +
                    int_to_little_endian(self.timestamp, 4) +
                    self.bits +
                    int_to_little_endian(self.nonce, 4)
                )
            )

            self.blockHash = header_string
            self.nonce += 1
            print(f" Mining Started: {self.nonce}", end='\r')

            # Check interrupt every 500 nonces
            if interrupt_flag is not None and self.nonce % 500 == 0:
                if interrupt_flag.mining_interrupted:
                    return  # stop — peer won

        self.blockHash = self.blockHash.to_bytes(32, 'little').hex()[::-1]
        self.bits = self.bits.hex()