import os
import json
import tempfile
import threading
import time as _time

# Per-process thread lock
_db_lock = threading.Lock()


def _acquire_file_lock(lock_path, timeout=10):
    """Cross-process file lock using atomic file creation."""
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                mtime = os.path.getmtime(lock_path)
                if _time.time() - mtime > 5:
                    os.unlink(lock_path)
                    continue
            except Exception:
                pass
            _time.sleep(0.01)
    return False


def _release_file_lock(lock_path):
    try:
        os.unlink(lock_path)
    except Exception:
        pass


class BaseDB:
    def __init__(self):
        self.basepath = "data"
        os.makedirs(self.basepath, exist_ok=True)
        self.filepath = os.path.join(self.basepath, self.filename + ".json")

    def read(self):
        if not os.path.exists(self.filepath):
            return []
        try:
            with open(self.filepath, 'r') as f:
                raw = f.read().strip()
            if not raw:
                return []
            return json.loads(raw)
        except Exception as e:
            print(f"DB READ ERROR ({self.filename}): {e}")
            return []

    def write(self, item):
        lock_path = self.filepath + '.lock'
        with _db_lock:
            if not _acquire_file_lock(lock_path):
                print(f"[COMPETE] Could not acquire file lock — discarding")
                return False
            try:
                data = self.read()
                if not isinstance(data, list):
                    data = []

                for new_block in item:
                    if not isinstance(new_block, dict):
                        continue
                    new_height = new_block.get('Height')
                    if new_height is not None:
                        existing = {b.get('Height') for b in data if isinstance(b, dict)}
                        if new_height in existing:
                            print(f"[COMPETE] Block #{new_height} already written — discarding")
                            return False

                data.extend(item)

                try:
                    serialized = json.dumps(data, indent=4)
                except Exception as e:
                    print(f"DB SERIALIZE ERROR: {e}")
                    return False

                dir_name = os.path.dirname(self.filepath) or '.'
                try:
                    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
                    try:
                        with os.fdopen(fd, 'w') as f:
                            f.write(serialized)
                        os.replace(tmp_path, self.filepath)
                        return True
                    except Exception:
                        try:
                            os.unlink(tmp_path)
                        except Exception:
                            pass
                        raise
                except Exception as e:
                    print(f"DB WRITE ERROR: {e}")
                    return False
            finally:
                _release_file_lock(lock_path)


class BlockChainDB(BaseDB):
    def __init__(self):
        self.filename = "blockchain"
        super().__init__()

    def lastBlock(self):
        data = self.read()
        return data[-1] if data else None


class AccountDB(BaseDB):
    def __init__(self):
        self.filename = "account"
        super().__init__()

    def lastBlock(self):
        data = self.read()
        return data[-1] if data else None

    def getActiveAccount(self):
        accounts = self.read()
        if not accounts:
            raise Exception("No account found! Run account.py first")
        return accounts[0]

    def getAccountByAddress(self, address):
        for acc in self.read():
            if acc['PublicAddress'] == address:
                return acc
        return None