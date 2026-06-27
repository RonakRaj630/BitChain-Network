import sys
import os
import json
import hashlib
import subprocess
import signal
import atexit
import time
import threading

import pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]  
sys.path.insert(0, str(ROOT))

from flask import (Flask, render_template, request, redirect, url_for, session, jsonify, make_response)
from dotenv import load_dotenv
from functools import wraps
from datetime import timedelta

from Blockchain.Backend.Core.database.database import AccountDB, BlockChainDB
from Blockchain.client.account import account as AccountCreator
from Blockchain.Backend.util.util import decode_Base58
from Blockchain.Frontend.otp_service import generate_otp, store_otp, verify_otp, send_otp_email

load_dotenv()

app = Flask(
    __name__,
    template_folder=str(ROOT / 'Blockchain' / 'Frontend' / 'templates'),
    static_folder=str(ROOT / 'Blockchain' / 'Frontend' / 'static'),
)

app.secret_key = os.environ.get('SECRET_KEY', '')
app.permanent_session_lifetime = timedelta(days=7)

@app.context_processor
def inject_session():
    return dict(session=session)

ALL_PROCESSES = {}
NEXT_PORT = 5001
LAST_HEARTBEAT = {}   # address -> timestamp
REGISTRY = {}         # p2p_addr ('ip:port') -> timestamp — all active miner nodes

_utxos        = None
_memPool      = None
_pending_txns = {}   # txns queued when no live mempool available

# ════════════════════════════════════════════════
# PROCESS CLEANUP
# ════════════════════════════════════════════════

def _kill_all_miners():
    for port, info in list(ALL_PROCESSES.items()):
        try:
            proc = info['process']
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try: info['process'].kill()
            except Exception: pass
    ALL_PROCESSES.clear()

atexit.register(_kill_all_miners)

# ── Heartbeat watcher — stops miner if no ping for 30 s ──
HEARTBEAT_TIMEOUT = 120   # seconds

# Track addresses that were auto-started so we can restart them if they crash
_MINER_CONFIGS = {}   # address -> {'args': [...], 'env': {...}}

def _heartbeat_watcher():
    while True:
        time.sleep(10)
        now = time.time()

        # ── Check 1: heartbeat timeout (tab closed without stopping miner) ──
        for address, last in list(LAST_HEARTBEAT.items()):
            if now - last > HEARTBEAT_TIMEOUT:
                running, _ = is_miner_running(address)
                if running:
                    print(f"💤 No heartbeat from {address[:12]}… — stopping miner")
                    _stop_miner(address)
                del LAST_HEARTBEAT[address]

        # ── Check 2: poll ALL_PROCESSES for dead processes ──
        for port, info in list(ALL_PROCESSES.items()):
            proc = info['process']
            if proc.poll() is not None:
                # Process died unexpectedly
                address = info.get('address', '')
                print(f"💀 Miner process on port {port} died (exit code {proc.poll()}) — cleaning up")
                del ALL_PROCESSES[port]

                # ── Auto-restart if miner config is saved ──
                if address in _MINER_CONFIGS:
                    print(f"🔄 Auto-restarting miner for {address[:12]}…")
                    try:
                        cfg = _MINER_CONFIGS[address]
                        _start_miner_process(address, cfg['port'])
                        print(f"✅ Miner for {address[:12]}… restarted on port {cfg['port']}")
                    except Exception as e:
                        print(f"❌ Auto-restart failed for {address[:12]}…: {e}")
                        del _MINER_CONFIGS[address]

_watcher = threading.Thread(target=_heartbeat_watcher, daemon=True)
_watcher.start()

# ════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def no_cache(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma']        = 'no-cache'
    response.headers['Expires']       = '0'
    return response

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('address'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper

def get_blockchain_data():
    return BlockChainDB().read() or []

def build_utxo_set_from_db():
    """
    Build UTXO set reading blockchain ONCE to avoid race conditions.
    Returns dict: txid -> JSON string (tx with only unspent outputs).
    """
    blocks = get_blockchain_data()  # read ONCE
    utxo = {}

    # Pass 1: collect all outputs
    for block in blocks:
        for tx in block.get('Txs', []):
            txid = tx.get('TxId', '')
            if txid:
                import copy
                utxo[txid] = copy.deepcopy(tx)

    # Pass 2: remove spent outputs
    for block in blocks:
        for tx in block.get('Txs', []):
            for inp in tx.get('tx_ins', []):
                spent_id  = inp.get('prev_tx', '')
                spent_idx = inp.get('prev_index', -1)
                if spent_id and spent_id != '0' * 64 and spent_id in utxo:
                    outs = utxo[spent_id].get('tx_outs', [])
                    if 0 <= spent_idx < len(outs):
                        outs[spent_idx] = None
                    if all(o is None for o in outs):
                        del utxo[spent_id]

    # Convert to JSON strings to match Manager dict format
    return {txid: json.dumps(tx) for txid, tx in utxo.items()}


def get_balance_from_db(address):
    """
    Compute balance from blockchain DB. Reads chain once for accuracy.
    """
    try:
        h160 = decode_Base58(address)
    except Exception:
        return 0, 0

    total = 0
    count = 0
    for txid, tx_json in build_utxo_set_from_db().items():
        try:
            tx = json.loads(tx_json) if isinstance(tx_json, str) else tx_json
            for out in tx.get('tx_outs', []):
                if out is None:
                    continue
                cmds = out.get('script_pubKey', {}).get('cmds', [])
                if len(cmds) > 2:
                    stored = bytes.fromhex(cmds[2]) if isinstance(cmds[2], str) else cmds[2]
                    if stored == h160:
                        total += out['amount']
                        count += 1
        except Exception as e:
            pass
    return total, count

def get_balance(address):
    if _utxos is not None:
        total = 0; count = 0
        try:
            h160 = decode_Base58(address)
            for txid in dict(_utxos):
                tx = json.loads(_utxos[txid])
                for out in tx['tx_outs']:
                    cmds = out['script_pubKey']['cmds']
                    if len(cmds) > 2 and bytes.fromhex(cmds[2]) == h160:
                        total += out['amount']
                        count += 1
        except Exception as e:
            print(f"Balance(mem) error: {e}")
        return total, count
    return get_balance_from_db(address)

def get_pending_locked(address):
    """
    Returns total satoshis currently locked in pending (unconfirmed) mempool
    transactions sent FROM this address.
    These coins are already reserved — subtract from available balance.
    """
    try:
        import os as _os
        from Blockchain.Backend.util.util import decode_Base58 as _d58
        h160 = _d58(address)
    except Exception:
        return 0

    locked = 0
    try:
        # Check pending_txns.json
        pending_file = _os.path.join('data', 'pending_txns.json')
        if _os.path.exists(pending_file) and _os.path.getsize(pending_file) > 0:
            with open(pending_file, 'r') as f:
                pending = json.load(f)
            blocks = get_blockchain_data()
            all_outs = {}   # (txid, idx) -> amount
            for block in blocks:
                for tx in block.get('Txs', []):
                    for idx, out in enumerate(tx.get('tx_outs', [])):
                        if out:
                            all_outs[(tx['TxId'], idx)] = out

            for txid, tx_dict in pending.items():
                # Check if this tx's inputs belong to our address
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
    except Exception as e:
        print(f"[PENDING] Error calculating locked amount: {e}")

    # Also check live mempool if available
    try:
        if _memPool is not None:
            blocks = get_blockchain_data()
            all_outs = {}
            for block in blocks:
                for tx in block.get('Txs', []):
                    for idx, out in enumerate(tx.get('tx_outs', [])):
                        if out:
                            all_outs[(tx['TxId'], idx)] = out
            for mp_tx in dict(_memPool).values():
                try:
                    tx_obj = mp_tx if hasattr(mp_tx, 'tx_ins') else None
                    if tx_obj:
                        for inp in tx_obj.tx_ins:
                            prev_tx  = inp.prev_tx.hex() if isinstance(inp.prev_tx, bytes) else inp.prev_tx
                            prev_idx = inp.prev_index
                            out = all_outs.get((prev_tx, prev_idx))
                            if out:
                                cmds = out.get('script_pubKey', {}).get('cmds', [])
                                if len(cmds) > 2:
                                    stored = bytes.fromhex(cmds[2]) if isinstance(cmds[2], str) else cmds[2]
                                    if stored == h160:
                                        locked += out['amount']
                except Exception:
                    pass
    except Exception:
        pass

    return locked


def get_all_transactions():
    blocks = get_blockchain_data()
    all_txs = {}
    for block in blocks:
        for tx in block.get('Txs', []):
            all_txs[tx.get('TxId')] = tx

    txns = []
    for block in blocks:
        for tx in block.get('Txs', []):
            t = dict(tx)
            t['block_height'] = block['Height']
            t['block_hash']   = block['BlockHeader']['blockHash']
            
            is_coinbase = False
            if t.get('tx_ins') and t['tx_ins'][0].get('prev_tx') == '0' * 64:
                is_coinbase = True
                t['is_coinbase'] = True
                
            input_amount = 0
            if not is_coinbase:
                for inp in t.get('tx_ins', []):
                    prev_tx = inp.get('prev_tx')
                    prev_idx = inp.get('prev_index')
                    if prev_tx in all_txs:
                        try:
                            input_amount += all_txs[prev_tx]['tx_outs'][prev_idx]['amount']
                        except Exception:
                            pass
                            
            output_amount = sum(out.get('amount', 0) for out in t.get('tx_outs', []))
            
            if is_coinbase:
                # Base reward is 3.125 BTC (312500000 Satoshis)
                t['fee'] = max(0, output_amount - 312500000)
                t['transfer_amount'] = output_amount
            else:
                t['fee'] = max(0, input_amount - output_amount) if input_amount > 0 else 0
                t['transfer_amount'] = t['tx_outs'][0]['amount'] if t.get('tx_outs') else 0
                
            txns.append(t)
    return txns

def get_blocks_mined_by(address):
    try:
        h160 = decode_Base58(address)
    except Exception:
        return 0
    count = 0
    for block in get_blockchain_data():
        txs = block.get('Txs', [])
        if not txs: continue
        outs = txs[0].get('tx_outs', [])
        if not outs: continue
        cmds = outs[0].get('script_pubKey', {}).get('cmds', [])
        if len(cmds) > 2:
            try:
                if bytes.fromhex(cmds[2]) == h160:
                    count += 1
            except Exception:
                pass
    return count

def is_miner_running(address):
    for port, info in list(ALL_PROCESSES.items()):
        if info.get('address') == address:
            if info['process'].poll() is None:
                return True, port
            else:
                del ALL_PROCESSES[port]
                return False, None
    return False, None

def get_mempool_count():
    count = 0
    if _memPool is not None:
        try: count += len(dict(_memPool))
        except Exception: pass
    # Also count pending txns waiting in file
    count += len(_pending_txns)
    try:
        pending_file = os.path.join('data', 'pending_txns.json')
        if os.path.exists(pending_file) and os.path.getsize(pending_file) > 0:
            with open(pending_file, 'r') as f:
                pending = json.load(f)
            count += len(pending)
    except Exception:
        pass
    return count

def get_stats():
    blocks   = get_blockchain_data()
    tx_count = sum(b.get('TxCount', 0) for b in blocks)
    return {
        'block_count':   len(blocks),
        'tx_count':      tx_count,
        'mempool_count': get_mempool_count(),
        'miner_count':   len(ALL_PROCESSES),
    }

# ════════════════════════════════════════════════
# AUTH ROUTES
# ════════════════════════════════════════════════

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('address'):
        running, _ = is_miner_running(session['address'])
        if running:
            return redirect(url_for('wallet'))
        session.clear()

    msg = ''

    # Step 2: OTP verification
    if request.method == 'POST' and request.form.get('otp_step') == '1':
        submitted_otp = request.form.get('otp', '').strip()
        pending_email = session.get('pending_email')
        pending_addr  = session.get('pending_address')
        remember      = session.get('pending_remember')

        if not pending_email:
            msg = '❌ Session expired. Please login again.'
            return no_cache(make_response(render_template('login.html', msg=msg)))

        ok, reason = verify_otp(pending_email, submitted_otp, 'login')
        if not ok:
            return no_cache(make_response(render_template(
                'otp_verify.html', msg=f'❌ {reason}',
                purpose='login', email=pending_email)))

        session.pop('pending_email', None)
        session.pop('pending_address', None)
        session.pop('pending_remember', None)
        if remember:
            session.permanent = True
        session['address'] = pending_addr
        session.pop('port', None)
        return no_cache(make_response(redirect(url_for('wallet'))))

    # Step 1: Address + Password
    if request.method == 'POST':
        address  = request.form.get('address', '').strip()
        password = request.form.get('password', '').strip()
        remember = request.form.get('remember')

        acc = AccountDB().getAccountByAddress(address)
        if not acc:
            msg = '❌ Address not found. Please register first.'
        else:
            stored = acc.get('password_hash', '')
            if stored and stored != hash_password(password):
                msg = '❌ Wrong password.'
            else:
                if not stored:
                    acc['password_hash'] = hash_password(password)
                    _save_account_update(acc)

                email = acc.get('email', '')
                if not email:
                    # No email — skip OTP, login directly
                    if remember:
                        session.permanent = True
                    session['address'] = address
                    session.pop('port', None)
                    return no_cache(make_response(redirect(url_for('wallet'))))

                # Send OTP
                otp  = generate_otp()
                store_otp(email, otp, 'login', address)
                sent = send_otp_email(email, otp, 'login')
                if not sent:
                    msg = '❌ Could not send OTP. Check email config.'
                else:
                    session['pending_email']    = email
                    session['pending_address']  = address
                    session['pending_remember'] = remember
                    masked = email[:2] + '***' + email[email.find('@'):]
                    return no_cache(make_response(render_template(
                        'otp_verify.html', msg='',
                        purpose='login', email=masked)))

    return no_cache(make_response(render_template('login.html', msg=msg)))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if session.get('address'):
        return redirect(url_for('wallet'))

    msg = ''
    if request.method == 'POST':
        address     = request.form.get('address', '').strip()
        private_key = request.form.get('privateKey', '').strip()
        password    = request.form.get('password', '').strip()
        confirm     = request.form.get('confirm_password', '').strip()
        email       = request.form.get('email', '').strip()

        if password != confirm:
            msg = '❌ Passwords do not match.'
        elif len(password) < 8:
            msg = '❌ Password must be at least 8 characters.'
        elif not address or not private_key:
            msg = '❌ Missing address or private key.'
        else:
            if AccountDB().getAccountByAddress(address):
                msg = '❌ This Bitcoin address is already registered. Please login.'
            elif email and any(
                acc.get('email', '').lower() == email.lower()
                for acc in AccountDB().read()
            ):
                msg = '❌ This email is already linked to another account.'
            else:
                AccountDB().write([{
                    'PublicAddress': address,
                    'privateKey':    int(private_key),
                    'email':         email,
                    'password_hash': hash_password(password),
                }])
                session['address'] = address
                session.pop('port', None)
                return no_cache(make_response(redirect(url_for('wallet'))))

    return no_cache(make_response(render_template('register.html', msg=msg, generated=None)))

@app.route('/logout', methods=['POST'])
def logout():
    address = session.get('address')
    session.clear()
    if address:
        LAST_HEARTBEAT.pop(address, None)
        _stop_miner(address)
    return no_cache(make_response(redirect(url_for('index'))))

@app.route('/resend_otp', methods=['POST'])
def resend_otp():
    """
    Resend OTP — invalidates previous OTP automatically
    because store_otp() overwrites the same email key.
    Works for both login and reset flows.
    """
    purpose = request.form.get('purpose', 'login')

    if purpose == 'login':
        email   = session.get('pending_email', '')
        address = session.get('pending_address', '')
    else:
        email   = session.get('reset_email', '')
        address = None

    if not email:
        # Session lost — redirect back to start
        return redirect(url_for('login') if purpose == 'login' else url_for('forgot'))

    # Generate fresh OTP — automatically invalidates previous one
    otp  = generate_otp()
    store_otp(email, otp, purpose, address)
    sent = send_otp_email(email, otp, purpose)

    masked      = email[:2] + '***' + email[email.find('@'):]
    resend_msg  = '✅ New OTP sent!' if sent else '❌ Failed to send. Check email config.'

    return no_cache(make_response(render_template(
        'otp_verify.html',
        msg        = '',
        resend_msg = resend_msg,
        purpose    = purpose,
        email      = masked,
    )))

@app.route('/forgot', methods=['GET', 'POST'])
def forgot():
    msg = ''

    # Step 3: Set new password after OTP verified
    if request.method == 'POST' and request.form.get('reset_step') == '2':
        new_pw  = request.form.get('password', '').strip()
        confirm = request.form.get('confirm_password', '').strip()
        address = request.form.get('address', '').strip()
        email   = session.get('reset_email', '')

        if not email:
            return no_cache(make_response(render_template('forgot.html', msg='❌ Session expired.')))
        if new_pw != confirm:
            return no_cache(make_response(render_template(
                'reset_password.html', msg='❌ Passwords do not match.',
                email=email, address=address)))
        if len(new_pw) < 8:
            return no_cache(make_response(render_template(
                'reset_password.html', msg='❌ Minimum 8 characters.',
                email=email, address=address)))

        acc = AccountDB().getAccountByAddress(address)
        if not acc or acc.get('email', '') != email:
            return no_cache(make_response(render_template(
                'reset_password.html', msg='❌ Address does not match this email.',
                email=email, address=address)))

        acc['password_hash'] = hash_password(new_pw)
        _save_account_update(acc)
        session.pop('reset_email', None)
        return no_cache(make_response(render_template(
            'login.html', msg='✅ Password reset successfully! Please login.')))

    # Step 2: OTP verification for reset
    if request.method == 'POST' and request.form.get('reset_step') == '1':
        submitted_otp = request.form.get('otp', '').strip()
        reset_email   = session.get('reset_email', '')

        if not reset_email:
            msg = '❌ Session expired.'
            return no_cache(make_response(render_template('forgot.html', msg=msg)))

        ok, reason = verify_otp(reset_email, submitted_otp, 'reset')
        if not ok:
            return no_cache(make_response(render_template(
                'otp_verify.html', msg=f'❌ {reason}',
                purpose='reset', email=reset_email)))

        return no_cache(make_response(render_template(
            'reset_password.html', msg='', email=reset_email, address='')))

    # Step 1: Enter email → send OTP
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        if not email:
            msg = '❌ Please enter your email address.'
        else:
            found = any(acc.get('email', '') == email for acc in AccountDB().read())
            if found:
                otp  = generate_otp()
                store_otp(email, otp, 'reset')
                sent = send_otp_email(email, otp, 'reset')
                if sent:
                    session['reset_email'] = email
                    masked = email[:2] + '***' + email[email.find('@'):]
                    return no_cache(make_response(render_template(
                        'otp_verify.html', msg='',
                        purpose='reset', email=masked)))
                else:
                    msg = '❌ Could not send OTP. Check email config.'
            else:
                msg = f'✅ If an account exists for {email}, an OTP has been sent.'

    return render_template('forgot.html', msg=msg)


# ════════════════════════════════════════════════
# PUBLIC PAGES
# ════════════════════════════════════════════════

@app.route('/')
def index():
    blocks        = get_blockchain_data()
    latest_blocks = list(reversed(blocks))[:5]
    return no_cache(make_response(render_template('index.html',
        latest_blocks=latest_blocks, stats=get_stats())))

@app.route('/blocks')
def blocks():
    return render_template('blocks.html', blocks=list(reversed(get_blockchain_data())))

@app.route('/block/<block_hash>')
def block_detail(block_hash):
    for block in get_blockchain_data():
        if block['BlockHeader']['blockHash'] == block_hash:
            return render_template('block_detail.html', block=_dict_to_ns(block))
    return redirect(url_for('blocks'))

@app.route('/transactions')
def transactions():
    return render_template('transactions.html',
                            transactions=list(reversed(get_all_transactions())))

@app.route('/tx/<txid>')
def tx_detail(txid):
    for tx in get_all_transactions():
        if tx.get('TxId') == txid:
            return render_template('tx_detail.html', tx=_dict_to_ns(tx))
    if _memPool:
        mp = dict(_memPool)
        if txid in mp:
            tx = mp[txid].to_dict()
            tx['block_height'] = None
            tx['block_hash']   = None
            return render_template('tx_detail.html', tx=_dict_to_ns(tx))
    return redirect(url_for('transactions'))

@app.route('/mempool')
def mempool():
    txns = []
    seen = set()
    
    utxos_db = build_utxo_set_from_db()

    current_addr = session.get('address')
    h160 = None
    if current_addr:
        try:
            h160 = decode_Base58(current_addr)
        except Exception:
            pass

    def _process_tx(tx_dict, txid):
        if txid in seen:
            return
        seen.add(txid)
        
        recipient_amount = 0
        is_sender = False
        is_receiver = False
        outs = tx_dict.get('tx_outs', [])
        ins = tx_dict.get('tx_ins', [])
        
        if outs:
            recipient_amount = outs[0]['amount']
            # Check receiver
            if h160:
                cmds = outs[0].get('script_pubKey', {}).get('cmds', [])
                if len(cmds) > 2:
                    stored = bytes.fromhex(cmds[2]) if isinstance(cmds[2], str) else cmds[2]
                    if stored == h160:
                        is_receiver = True
            
        # Check sender from inputs
        if h160 and ins:
            for inp in ins:
                cmds = inp.get('script_sig', {}).get('cmds', [])
                if len(cmds) > 1:
                    pubkey = bytes.fromhex(cmds[1]) if isinstance(cmds[1], str) else cmds[1]
                    try:
                        from Blockchain.Backend.util.util import hash160
                        if hash160(pubkey) == h160:
                            is_sender = True
                            break
                    except Exception:
                        pass
        
        # Calculate fee
        input_amount = 0
        for inp in ins:
            prev_tx = inp.get('prev_tx', '')
            if isinstance(prev_tx, str):
                pass
            elif isinstance(prev_tx, bytes):
                prev_tx = prev_tx.hex()
                
            prev_idx = inp.get('prev_index', 0)
            if prev_tx in utxos_db:
                try:
                    ptx = json.loads(utxos_db[prev_tx])
                    input_amount += ptx['tx_outs'][prev_idx]['amount']
                except Exception:
                    pass
            else:
                # If an input is not in UTXO db, it's already spent (mined) or invalid!
                return
        
        output_amount = sum([o.get('amount', 0) for o in outs])
        fee_amount = input_amount - output_amount if input_amount > 0 else 0
                            
        txns.append({
            'txid': txid, 
            'amount': recipient_amount, 
            'fee': fee_amount, 
            'is_sender': is_sender,
            'is_receiver': is_receiver
        })

    # 1. From live miner mempool via its API (most accurate)
    for port, info in ALL_PROCESSES.items():
        if info['process'].poll() is None:
            try:
                import urllib.request
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/mempool_txns", timeout=2
                ) as resp:
                    data = json.loads(resp.read())
                    for item in data.get('txns', []):
                        _process_tx(item['tx_dict'], item['txid'])
            except Exception:
                pass

    # 2. From in-process _memPool (when run as miner app)
    if _memPool:
        for txid, tx_obj in dict(_memPool).items():
            try:
                _process_tx(tx_obj.to_dict(), txid)
            except Exception:
                pass

    # 3. From pending file
    try:
        pending_file = os.path.join('data', 'pending_txns.json')
        if os.path.exists(pending_file) and os.path.getsize(pending_file) > 0:
            with open(pending_file, 'r') as f:
                pending = json.load(f)
            for txid, tx_dict in pending.items():
                _process_tx(tx_dict, txid)
    except Exception:
        pass

    # Get selected txs from current active miner
    selected_txs = set()
    current_addr = session.get('address')
    if current_addr:
        for port, info in ALL_PROCESSES.items():
            if info.get('address') == current_addr and info['process'].poll() is None:
                try:
                    import urllib.request
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/get_selected_txs", timeout=1) as resp:
                        data = json.loads(resp.read())
                        selected_txs.update(data.get('selected', []))
                except Exception:
                    pass

    for t in txns:
        t['is_selected'] = t['txid'] in selected_txs

    return render_template('mempool.html', txns=txns)

@app.route('/mempool/mine_selected', methods=['POST'])
def mine_selected():
    txids = request.form.getlist('txids')
    current_addr = session.get('address')
    if current_addr and txids:
        for port, info in ALL_PROCESSES.items():
            if info.get('address') == current_addr and info['process'].poll() is None:
                try:
                    import urllib.request
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/select_tx",
                        data=json.dumps({'txids': txids}).encode('utf-8'),
                        headers={'Content-Type': 'application/json'}
                    )
                    with urllib.request.urlopen(req, timeout=1) as resp:
                        pass
                except Exception:
                    pass
    return redirect(url_for('mempool'))

@app.route('/mempool/cancel_verification', methods=['POST'])
def cancel_verification():
    txid = request.form.get('txid')
    current_addr = session.get('address')
    if current_addr and txid:
        for port, info in ALL_PROCESSES.items():
            if info.get('address') == current_addr and info['process'].poll() is None:
                try:
                    import urllib.request
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/deselect_tx",
                        data=json.dumps({'txid': txid}).encode('utf-8'),
                        headers={'Content-Type': 'application/json'}
                    )
                    with urllib.request.urlopen(req, timeout=1) as resp:
                        pass
                except Exception:
                    pass
    return redirect(url_for('mempool'))

@app.route('/mempool/tx/<txid>')
def mempool_tx(txid):
    return redirect(url_for('tx_detail', txid=txid))

@app.route('/address/<address>')
def address_detail(address):
    txns = []; spent = 0; tx_count = 0
    try:
        h160 = decode_Base58(address)
        for tx in get_all_transactions():
            involved = False
            for out in tx.get('tx_outs', []):
                cmds = out.get('script_pubKey', {}).get('cmds', [])
                if len(cmds) > 2 and bytes.fromhex(cmds[2]) == h160:
                    involved = True
            for inp in tx.get('tx_ins', []):
                if inp.get('prev_tx') != '0' * 64:
                    spent += 1; involved = True
            if involved:
                tx_count += 1; txns.append(tx)
    except Exception as e:
        print(f"Address detail error: {e}")
    balance, _ = get_balance_from_db(address)
    if balance < 0: balance = 0
    return render_template('address.html',
        address=address,
        stats={'tx_count': tx_count, 'spent': spent, 'balance': balance},
        txns=[_dict_to_ns(t) for t in txns])

@app.route('/search')
def search():
    q = request.args.get('q', '').strip()
    if not q: return redirect(url_for('index'))
    for block in get_blockchain_data():
        if block['BlockHeader']['blockHash'] == q:
            return redirect(url_for('block_detail', block_hash=q))
    for tx in get_all_transactions():
        if tx.get('TxId') == q:
            return redirect(url_for('tx_detail', txid=q))
    if q.startswith('1') and len(q) > 25:
        return redirect(url_for('address_detail', address=q))
    return render_template('search.html', query=q, result_type=None)

# ════════════════════════════════════════════════
# PROTECTED PAGES
# ════════════════════════════════════════════════

@app.route('/wallet', methods=['GET', 'POST'])
@login_required
def wallet():
    from Blockchain.client.sentBTC import sendBTC
    from Blockchain.Backend.Core.Transaction import Transaction

    send_msg = ''
    address  = session['address']

    # Auto-restart miner if process died
    running, port = is_miner_running(address)

    if request.method == 'POST':
        to_addr    = request.form.get('toAddress', '').strip()
        amount     = request.form.get('Amount', type=float)
        fee_amount = request.form.get('Fee', type=float)

        print(f"[SEND] from={address[:12]} to={to_addr[:12] if to_addr else None} amount={amount} fee={fee_amount}")
        print(f"[SEND] _utxos={_utxos is not None} _memPool={_memPool is not None} port={session.get('port')}")

        if not to_addr or not amount:
            send_msg = '❌ Please fill in all fields.'
        elif amount <= 0:
            send_msg = '❌ Amount must be greater than 0.'
        else:
            try:
                utxos_to_use = build_utxo_set_from_db()

                # Remove UTXOs already spent by unconfirmed mempool transactions
                # This prevents double-spend while TX is awaiting confirmation
                mempool_spent = set()
                if _memPool:
                    for _, mp_tx in dict(_memPool).items():
                        try:
                            d = mp_tx.to_dict()
                            for inp in d.get('tx_ins', []):
                                sid = inp.get('prev_tx', '')
                                sidx = inp.get('prev_index', -1)
                                if sid and sid != '0' * 64:
                                    mempool_spent.add((sid, sidx))
                        except Exception:
                            pass
                # Also check pending REST pushes via miner port
                miner_port = session.get('port')
                if miner_port:
                    try:
                        import urllib.request
                        with urllib.request.urlopen(
                            f'http://127.0.0.1:{miner_port}/api/mempool_txns',
                            timeout=1
                        ) as resp:
                            mp_data = json.loads(resp.read())
                            for txid in [t['txid'] for t in mp_data.get('txns', [])]:
                                # Mark all inputs of mempool txs as spent
                                pass  # txid only — full input data not exposed yet
                    except Exception:
                        pass
                print(f"[SEND] UTXO set size: {len(utxos_to_use)}")

                if not utxos_to_use:
                    send_msg = '❌ No UTXOs found — mine some blocks first.'
                else:
                    sender = sendBTC(address, to_addr, amount, utxos_to_use, fee_amount=fee_amount)
                    tx_obj = sender.prepareTransaction()
                    print(f"[SEND] prepareTransaction={tx_obj}")

                    if not tx_obj:
                        bal, _   = get_balance(address)
                        locked   = get_pending_locked(address)
                        available = max(0, bal - locked)
                        if locked > 0:
                            send_msg = (
                                f'❌ Insufficient balance. '
                                f'Confirmed: {bal/100000000:.4f} BTC — '
                                f'Locked in pending: {locked/100000000:.4f} BTC — '
                                f'Available: {available/100000000:.4f} BTC'
                            )
                        else:
                            send_msg = f'❌ Insufficient balance. Available: {bal/100000000:.4f} BTC'
                    elif isinstance(tx_obj, Transaction):
                        spk      = sender.scriptPubKey(address)
                        verified = all(tx_obj.verify_input(i, spk) for i in range(len(tx_obj.tx_ins)))
                        print(f"[SEND] verified={verified}")

                        if verified:
                            pushed = False

                            # Option 1: live mempool on this process
                            if _memPool is not None:
                                _memPool[tx_obj.TxId] = tx_obj
                                pushed = True
                                print("[SEND] added to local _memPool")

                            # Option 2: push to miner subprocess by looking up ALL_PROCESSES
                            if not pushed:
                                miner_port = None
                                for p, info in ALL_PROCESSES.items():
                                    if info.get('address') == address:
                                        miner_port = p
                                        break
                                print(f"[SEND] miner_port={miner_port} ALL_PROCESSES={list(ALL_PROCESSES.keys())}")
                                if miner_port:
                                    try:
                                        import urllib.request as _ur
                                        data = json.dumps(tx_obj.to_dict()).encode()
                                        req  = _ur.Request(
                                            f'http://127.0.0.1:{miner_port}/api/receive_tx',
                                            data=data,
                                            headers={'Content-Type': 'application/json'},
                                            method='POST'
                                        )
                                        _ur.urlopen(req, timeout=5)
                                        pushed = True
                                        print(f"[SEND] ✅ pushed to miner port {miner_port}")
                                    except Exception as pe:
                                        print(f"[SEND] ❌ REST push failed: {pe}")

                            # Option 3: save to file as last resort
                            if not pushed:
                                _save_pending_tx(tx_obj)
                                print("[SEND] saved to pending file")

                            send_msg = f'✅ Transaction {tx_obj.TxId[:16]}... sent to mempool!'
                        else:
                            send_msg = '❌ Signature verification failed.'
            except Exception as e:
                import traceback; traceback.print_exc()
                send_msg = f'❌ Error: {e}'

    balance, utxo_count = get_balance_from_db(address)
    if balance < 0: balance = 0
    blocks_mined        = get_blocks_mined_by(address)
    mempool_count       = get_mempool_count()

    recent_txns = []
    try:
        h160 = decode_Base58(address)
        for tx in reversed(get_all_transactions()):
            is_coinbase = (tx.get('tx_ins', [{}])[0].get('prev_tx', '') == '0' * 64)

            # Check if this address is a SENDER (its output is used as input)
            is_sender = False
            if not is_coinbase:
                for inp in tx.get('tx_ins', []):
                    prev_txid = inp.get('prev_tx', '')
                    # Look up the previous tx to see if its output went to us
                    for block in get_blockchain_data():
                        for prev_tx in block.get('Txs', []):
                            if prev_tx.get('TxId') == prev_txid:
                                idx = inp.get('prev_index', 0)
                                outs = prev_tx.get('tx_outs', [])
                                if idx < len(outs) and outs[idx]:
                                    cmds = outs[idx].get('script_pubKey', {}).get('cmds', [])
                                    if len(cmds) > 2 and bytes.fromhex(cmds[2]) == h160:
                                        is_sender = True

            if is_sender:
                # Show what we sent (outputs NOT going back to us)
                sent_amount = 0
                for out in tx.get('tx_outs', []):
                    cmds = out.get('script_pubKey', {}).get('cmds', [])
                    if len(cmds) > 2 and bytes.fromhex(cmds[2]) != h160:
                        sent_amount += out['amount']
                if sent_amount > 0:
                    recent_txns.append({
                        'txid':         tx['TxId'],
                        'type':         'sent',
                        'amount':       sent_amount,
                        'fee':          tx.get('fee', 0),
                        'is_coinbase':  tx.get('is_coinbase', False),
                        'block_height': tx.get('block_height', '—'),
                    })
            else:
                # Check if we received anything
                for out in tx.get('tx_outs', []):
                    cmds = out.get('script_pubKey', {}).get('cmds', [])
                    if len(cmds) > 2 and bytes.fromhex(cmds[2]) == h160:
                        recent_txns.append({
                            'txid':         tx['TxId'],
                            'type':         'received',
                            'amount':       out['amount'],
                            'fee':          tx.get('fee', 0),
                            'is_coinbase':  tx.get('is_coinbase', False),
                            'block_height': tx.get('block_height', '—'),
                        })
                        break

            if len(recent_txns) >= 5:
                break
    except Exception as e:
        print(f"Recent txns error: {e}")

    return no_cache(make_response(render_template('wallet.html',
        balance       = balance / 100000000,
        utxo_count    = utxo_count,
        blocks_mined  = blocks_mined,
        mempool_count = mempool_count,
        send_msg      = send_msg,
        recent_txns   = recent_txns,
        mining_active = running,
    )))

@app.route('/cancel_tx/<txid>', methods=['POST'])
@login_required
def cancel_tx(txid):
    # 1. Delete from _memPool
    if _memPool is not None and txid in _memPool:
        try:
            del _memPool[txid]
        except Exception:
            pass
            
    # 2. Delete from pending_txns.json
    pending_file = os.path.join('data', 'pending_txns.json')
    if os.path.exists(pending_file):
        for attempt in range(10):
            try:
                with open(pending_file, 'r') as f:
                    pending = json.load(f)
                if txid in pending:
                    del pending[txid]
                    with open(pending_file, 'w') as f:
                        json.dump(pending, f, indent=4)
                    print(f"✅ Successfully deleted {txid} from pending_txns.json")
                else:
                    print(f"⚠️ {txid} not found in pending_txns.json")
                break
            except Exception as e:
                import time
                time.sleep(0.1)
                if attempt == 9:
                    print(f"❌ Error deleting from pending_txns.json after retries: {e}")

    # 3. Broadcast delete to all miner instances
    print(f"📡 Broadcasting cancel to {len(ALL_PROCESSES)} miners...")
    for port, info in ALL_PROCESSES.items():
        if info['process'].poll() is None:
            try:
                import urllib.request as _ur
                req = _ur.Request(
                    f'http://127.0.0.1:{port}/api/cancel_tx',
                    data=json.dumps({'txid': txid}).encode(),
                    headers={'Content-Type': 'application/json'},
                    method='POST'
                )
                _ur.urlopen(req, timeout=2)
                print(f"✅ Successfully broadcasted cancel to miner on port {port}")
            except Exception as e:
                print(f"❌ Error broadcasting cancel to port {port}: {e}")

    return redirect(url_for('mempool'))

# ════════════════════════════════════════════════
# API — polled by JS
# ════════════════════════════════════════════════

@app.route('/api/stats')
def api_stats():
    return jsonify(get_stats())

@app.route('/api/wallet_stats')
@login_required
def api_wallet_stats():
    address      = session['address']
    running, port = is_miner_running(address)

    # If miner is running, proxy to its wallet_stats for live data
    if running and port:
        try:
            import urllib.request
            with urllib.request.urlopen(
                f'http://127.0.0.1:{port}/api/wallet_stats', timeout=2
            ) as resp:
                data = json.loads(resp.read())
                data['mining_active'] = True
                data['block_count']   = len(get_blockchain_data())
                return jsonify(data)
        except Exception:
            pass  # fall through to DB read

    # Miner not running or proxy failed — read from DB
    balance, utxo_count = get_balance(address)
    blocks_mined        = get_blocks_mined_by(address)
    return jsonify({
        'balance':       f"{balance / 100000000:.8f}",
        'balance_sat':   balance,
        'utxo_count':    utxo_count,
        'blocks_mined':  blocks_mined,
        'mempool_count': get_mempool_count(),
        'mining_active': running,
        'block_count':   len(get_blockchain_data()),
    })

@app.route('/api/generate_keys')
def api_generate_keys():
    acc = AccountCreator()
    acc.createKeys()
    return jsonify({'address': acc.PublicAddress, 'privateKey': str(acc.privateKey)})

@app.route('/api/heartbeat', methods=['POST'])
@login_required
def api_heartbeat():
    """Called every 10 s — keeps session alive, does NOT auto-restart miner."""
    LAST_HEARTBEAT[session['address']] = time.time()
    return jsonify({'ok': True})

@app.route('/api/start_miner', methods=['POST'])
@login_required
def api_start_miner():
    """Manually start mining — called by Start button."""
    address = session['address']
    running, port = is_miner_running(address)
    if running:
        return jsonify({'ok': True, 'port': port, 'already': True})
    try:
        port = _get_or_start_miner(address)
        session['port'] = port
        LAST_HEARTBEAT[address] = time.time()
        return jsonify({'ok': True, 'port': port})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

@app.route('/api/stop_miner', methods=['POST'])
@login_required
def api_stop_miner():
    """Stop mining — called by Stop button or tab close beacon."""
    address = session['address']
    LAST_HEARTBEAT.pop(address, None)
    _stop_miner(address)
    return jsonify({'ok': True})

@app.route('/api/mining_status')
@login_required
def api_mining_status():
    running, port = is_miner_running(session['address'])
    return jsonify({'running': running, 'port': port})




@app.route('/api/receive_tx', methods=['POST'])
def api_receive_tx():
    """
    Called by the login server (port 5000) to push a signed transaction
    directly into this miner's live mempool.
    Only works when this app instance IS the miner (i.e. _memPool is not None).
    """
    if _memPool is None:
        return jsonify({'ok': False, 'error': 'No live mempool on this port'}), 400
    try:
        from Blockchain.Backend.Core.Transaction import Transaction, Tx_In, Tx_Out
        from Blockchain.Backend.Core.Script import script as Script
        tx_dict = request.get_json(force=True)

        tx_ins = []
        for inp in tx_dict['tx_ins']:
            prev_tx  = bytes.fromhex(inp['prev_tx'])
            cmds_sig = [bytes.fromhex(c) if isinstance(c, str) else c
                        for c in inp['script_sig']['cmds']]
            tx_ins.append(Tx_In(prev_tx, inp['prev_index'], Script(cmds_sig)))

        tx_outs = []
        for out in tx_dict['tx_outs']:
            cmds_pk = []
            for c in out['script_pubKey']['cmds']:
                if isinstance(c, int):
                    cmds_pk.append(c)
                else:
                    cmds_pk.append(bytes.fromhex(c))
            tx_outs.append(Tx_Out(out['amount'], Script(cmds_pk)))

        tx = Transaction(tx_dict['version'], tx_ins, tx_outs, tx_dict['locktime'])
        tx.TxId = tx_dict['TxId']
        _memPool[tx.TxId] = tx
        print(f"✅ Received tx from login server: {tx.TxId[:16]}...")
        return jsonify({'ok': True, 'txid': tx.TxId})
    except Exception as e:
        print(f"❌ receive_tx error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/balance')
@login_required
def balance_endpoint():
    # Always read from DB for accuracy — never from miner memory cache
    bal, utxo_count = get_balance_from_db(session['address'])
    # Never return negative balance
    if bal < 0:
        bal = 0
    return jsonify({
        'address':          session['address'],
        'balance':          bal / 100000000,
        'balance_satoshis': bal,
        'utxo_count':       utxo_count,
    })

# ════════════════════════════════════════════════
# INTERNALS
# ════════════════════════════════════════════════

def _save_pending_tx(tx_obj):
    """
    When run.py is the login server (port 5000) and the miner subprocess
    has its own mempool, we write the signed transaction to a pending JSON
    file. The miner reads this file and injects the tx into its mempool.
    """
    import copy
    pending_file = os.path.join('data', 'pending_txns.json')
    try:
        if os.path.exists(pending_file) and os.path.getsize(pending_file) > 0:
            with open(pending_file, 'r') as f:
                pending = json.load(f)
        else:
            pending = {}
        pending[tx_obj.TxId] = tx_obj.to_dict()
        fd, tmp = __import__('tempfile').mkstemp(dir='data', suffix='.tmp')
        with os.fdopen(fd, 'w') as f:
            json.dump(pending, f, indent=2)
        os.replace(tmp, pending_file)
        _pending_txns[tx_obj.TxId] = tx_obj
        print(f"✅ Pending tx saved: {tx_obj.TxId[:16]}...")
    except Exception as e:
        print(f"❌ Could not save pending tx: {e}")


def _save_account_update(acc):
    db = AccountDB()
    accounts = db.read()
    for i, a in enumerate(accounts):
        if a['PublicAddress'] == acc['PublicAddress']:
            accounts[i] = acc; break
    with open(db.filepath, 'w') as f:
        json.dump(accounts, f, indent=4)

def _get_or_start_miner(address):
    global NEXT_PORT
    running, port = is_miner_running(address)
    if running:
        return port
    port = NEXT_PORT
    NEXT_PORT += 1
    _start_miner_process(address, port)
    return port

def _start_miner_process(address, port):
    script = os.path.join('Blockchain', 'Backend', 'Core', 'blockchain.py')
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0

    # Each miner gets its own P2P port = wallet port + 1000
    p2p_port = port + 1000

    # ── Auto-discovery: collect all currently active miners' P2P addresses ──
    existing_p2p = [
        f"127.0.0.1:{info['p2p_port']}"
        for info in ALL_PROCESSES.values()
        if info['process'].poll() is None   # only alive miners
    ]

    env = os.environ.copy()
    env['P2P_PORT']  = str(p2p_port)
    env['P2P_PEERS'] = ','.join(existing_p2p)   # ← injected into new miner

    process = subprocess.Popen(
        [sys.executable, script, '--port', str(port), '--address', address],
        creationflags=flags,
        env=env,
    )
    ALL_PROCESSES[port] = {'address': address, 'process': process, 'port': port, 'p2p_port': p2p_port}
    _MINER_CONFIGS[address] = {'port': port, 'p2p_port': p2p_port}
    print(f"✅ Miner {address[:12]}… started | wallet:{port} P2P:{p2p_port} | auto-peers:{existing_p2p}")

    # ── Tell every existing miner to connect back to the new miner ──
    import urllib.request as _ur
    new_peer = f"127.0.0.1:{p2p_port}"
    import time as _t
    _t.sleep(2)   # give new miner time to start its P2P server
    for existing_port, info in list(ALL_PROCESSES.items()):
        if existing_port == port:
            continue
        if info['process'].poll() is not None:
            continue
        try:
            req = _ur.Request(
                f"http://127.0.0.1:{existing_port}/api/p2p_add_peer",
                data=json.dumps({'peer': new_peer}).encode(),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            _ur.urlopen(req, timeout=3)
            print(f"[P2P] 🔗 Told miner on port {existing_port} to connect to {new_peer}")
        except Exception as e:
            print(f"[P2P] ⚠️  Could not notify miner {existing_port}: {e}")

@app.route('/api/p2p_status')
@login_required
def api_p2p_status():
    """
    Returns live P2P info for the logged-in user's miner.
    Called by wallet.html every 5s to update the P2P panel.
    """
    address = session.get('address')
    running, port = is_miner_running(address)
    if not running:
        return jsonify({'miner_running': False, 'peer_count': 0, 'peers': [], 'p2p_port': None, 'blocks_received': 0})

    info     = ALL_PROCESSES.get(port, {})
    p2p_port = info.get('p2p_port')

    try:
        import urllib.request as _ur
        resp = _ur.urlopen(f"http://127.0.0.1:{port}/api/p2p_info", timeout=2)
        data = json.loads(resp.read())
        data['miner_running'] = True
        data['p2p_port']      = p2p_port
        return jsonify(data)
    except Exception:
        return jsonify({'miner_running': True, 'peer_count': 0, 'peers': [], 'p2p_port': p2p_port, 'blocks_received': 0})


def _stop_miner(address):
    # Remove from auto-restart config — this is intentional stop
    _MINER_CONFIGS.pop(address, None)
    for port, info in list(ALL_PROCESSES.items()):
        if info.get('address') == address:
            try:
                proc = info['process']
                if os.name == 'nt':
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try: info['process'].kill()
                except Exception: pass
            del ALL_PROCESSES[port]
            print(f"🛑 Miner {address[:12]}… stopped")
            return

def _dict_to_ns(d):
    from types import SimpleNamespace
    if isinstance(d, dict):
        return SimpleNamespace(**{k: _dict_to_ns(v) for k, v in d.items()})
    if isinstance(d, list):
        return [_dict_to_ns(i) for i in d]
    return d

@app.route('/api/p2p_register', methods=['POST'])
def api_p2p_register():
    """
    Called automatically by every miner's P2PNode on startup.
    - Adds the caller to REGISTRY
    - Returns the full list of currently active peer addresses
    - Notifies all existing peers about the new node
    Body JSON: { "p2p_addr": "192.168.1.5:6001" }
    """
    data     = request.get_json(force=True) or {}
    new_addr = data.get('p2p_addr', '').strip()
    if not new_addr:
        return jsonify({'ok': False, 'error': 'Missing p2p_addr'}), 400

    # Get existing peers BEFORE adding the new one (so we don't return it to itself)
    existing = [addr for addr in REGISTRY if addr != new_addr]

    # Register the new node
    REGISTRY[new_addr] = time.time()
    print(f"[Registry] ➕ Registered: {new_addr}  |  total nodes: {len(REGISTRY)}")

    # Tell all existing miners about the new peer so they connect immediately
    for addr in existing:
        _notify_peer_of_new_node(addr, new_addr)

    return jsonify({'ok': True, 'peers': existing})


@app.route('/api/p2p_unregister', methods=['POST'])
def api_p2p_unregister():
    """
    Called by P2PNode.stop() when a miner shuts down.
    Body JSON: { "p2p_addr": "192.168.1.5:6001" }
    """
    data = request.get_json(force=True) or {}
    addr = data.get('p2p_addr', '').strip()
    if addr in REGISTRY:
        del REGISTRY[addr]
        print(f"[Registry] ➖ Unregistered: {addr}  |  total nodes: {len(REGISTRY)}")
    return jsonify({'ok': True})


@app.route('/api/p2p_nodes')
def api_p2p_nodes():
    """Public endpoint — returns all currently registered P2P nodes."""
    return jsonify({'nodes': list(REGISTRY.keys()), 'count': len(REGISTRY)})


def _notify_peer_of_new_node(existing_peer_addr: str, new_node_addr: str):
    """
    Tell an existing miner's P2P TCP server about a newly joined node
    by hitting its miner wallet HTTP API so it connects immediately.
    existing_peer_addr = 'ip:p2p_port'  e.g. '192.168.1.5:6001'
    """
    # Find the wallet HTTP port for this P2P address
    for port, info in ALL_PROCESSES.items():
        p2p_port = info.get('p2p_port')
        if p2p_port and f":{p2p_port}" in existing_peer_addr:
            try:
                import urllib.request as _ur
                req = _ur.Request(
                    f"http://127.0.0.1:{port}/api/p2p_connect",
                    data=json.dumps({'peer': new_node_addr}).encode(),
                    headers={'Content-Type': 'application/json'},
                    method='POST'
                )
                _ur.urlopen(req, timeout=2)
                print(f"[Registry] 📡 Notified {existing_peer_addr} → connect to {new_node_addr}")
            except Exception as e:
                print(f"[Registry] ⚠️  Could not notify {existing_peer_addr}: {e}")
            return


@app.route('/api/add_peer', methods=['POST'])
@login_required
def api_add_peer():
    """
    Manual peer add (kept for backwards compatibility / admin use).
    POST body JSON: { "peer": "192.168.1.5:6001" }
    """
    data = request.get_json(force=True) or {}
    peer = data.get('peer', '').strip()
    if not peer:
        return jsonify({'ok': False, 'error': 'Missing peer address'}), 400

    address = session['address']
    for port, info in ALL_PROCESSES.items():
        if info.get('address') == address and info['process'].poll() is None:
            try:
                import urllib.request as _ur
                req = _ur.Request(
                    f"http://127.0.0.1:{port}/api/p2p_connect",
                    data=json.dumps({'peer': peer}).encode(),
                    headers={'Content-Type': 'application/json'},
                    method='POST'
                )
                _ur.urlopen(req, timeout=3)
                return jsonify({'ok': True, 'peer': peer})
            except Exception as e:
                return jsonify({'ok': False, 'error': str(e)}), 500

    return jsonify({'ok': False, 'error': 'No running miner found'}), 400


# ════════════════════════════════════════════════
# ENTRY POINTS
# ════════════════════════════════════════════════

def minerApp(utxos, memoryPool, port, address):
    global _utxos, _memPool
    _utxos   = utxos
    _memPool = memoryPool
    os.environ['MINER_ADDRESS'] = address
    app.run(host='0.0.0.0', port=port, debug=False)

def main(utxos=None, memoryPool=None, port=5000):
    pass

if __name__ == '__main__':
    print('✅ BitChain login server starting on http://127.0.0.1:5000')
    app.run(host='0.0.0.0', port=5000, debug=False)