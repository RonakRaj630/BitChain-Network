import sys
import secrets
import pathlib
from Blockchain.Backend.Core.EllepticCurve.EllepticCurve import Sha256Point
from Blockchain.Backend.util.util import hash160, hash256
from Blockchain.Backend.Core.database.database import AccountDB

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

class account:
    def createKeys(self):
        Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
        Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
        G = Sha256Point(Gx, Gy)

        self.privateKey = secrets.randbits(256)
        UnCompressedPublicKey = self.privateKey * G
        xPoint = UnCompressedPublicKey.x
        yPoint = UnCompressedPublicKey.y

        if yPoint.num % 2 == 0:
            compressKey = b'\x02' + xPoint.num.to_bytes(32, 'big')
        else:
            compressKey = b'\x03' + xPoint.num.to_bytes(32, 'big')

        Hash_160 = hash160(compressKey)
        main_prefix = b'\x00'
        newAddr = main_prefix + Hash_160
        checksum = hash256(newAddr)[:4]
        newAddr = newAddr + checksum

        Base58_Alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
        count = 0
        for i in newAddr:
            if i == 0:
                count += 1
            else:
                break

        numeric = int.from_bytes(newAddr, 'big')
        prefix = '1' * count
        result = ''
        while numeric > 0:
            numeric, mod = divmod(numeric, 58)
            result = Base58_Alphabet[mod] + result

        self.PublicAddress = prefix + result

        print(f'✅ Private Key: {self.privateKey}')
        print(f'✅ Public Address: {self.PublicAddress}')
        return self

    def setActive(self, PublicAddress):
        """
        Switch active mining account by moving
        selected address to front of AccountDB
        """
        db = AccountDB()
        accounts = db.read()

        # ✅ Find the account with matching address
        selectedAccount = None
        remainingAccounts = []

        for acc in accounts:
            if acc['PublicAddress'] == PublicAddress:
                selectedAccount = acc
            else:
                remainingAccounts.append(acc)

        if not selectedAccount:
            print(f"❌ Account {PublicAddress} not found!")
            return False

        # ✅ Put selected account FIRST
        # getMinerAccount() always reads accounts[0]
        newOrder = [selectedAccount] + remainingAccounts

        # ✅ Rewrite database with new order
        try:
            import json
            with open(db.filepath, 'w') as f:
                json.dump(newOrder, f, indent=4)
            print(f"✅ Active account switched to: {PublicAddress}")
            return True
        except Exception as e:
            print(f"❌ Error switching account: {e}")
            return False

    def listAccounts(self):
        """Show all saved accounts"""
        accounts = AccountDB().read()
        if not accounts:
            print("❌ No accounts found!")
            return

        print("\n📋 All Accounts:")
        print("-" * 50)
        for i, acc in enumerate(accounts):
            active = "🟢 ACTIVE" if i == 0 else "⚪"
            print(f"{active} [{i+1}] {acc['PublicAddress']}")
        print("-" * 50)


if __name__ == "__main__":
    import sys

    # ✅ Run with argument to switch account
    # python account.py --new       → create new account
    # python account.py --list      → list all accounts  
    # python account.py --switch <address> → switch active account

    if len(sys.argv) > 1:
        if sys.argv[1] == '--new':
            acc = account()
            acc.createKeys()
            AccountDB().write([{
                'PublicAddress': acc.PublicAddress,
                'privateKey': acc.privateKey
            }])
            print(f"✅ New account created and saved!")

        elif sys.argv[1] == '--list':
            account().listAccounts()

        elif sys.argv[1] == '--switch' and len(sys.argv) > 2:
            address = sys.argv[2]
            account().setActive(address)

        else:
            print("Usage:")
            print("  python account.py --new              → Create new account")
            print("  python account.py --list             → List all accounts")
            print("  python account.py --switch <address> → Switch active account")

    else:
        # ✅ Default - create new account
        acc = account()
        acc.createKeys()
        AccountDB().write([{
            'PublicAddress': acc.PublicAddress,
            'privateKey': acc.privateKey
        }])
        print(f"✅ Account saved to database!")