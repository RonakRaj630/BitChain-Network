# 🚀 BitChain Network
**A Blockchain-Based Cryptocurrency Simulation System built using Python and Flask.**

BitChain Network is an educational blockchain implementation that demonstrates the core working principles of cryptocurrencies such as Bitcoin. The project includes wallet generation, secure transactions, Proof-of-Work mining, SHA-256 hashing, UTXO management, Merkle Tree construction, peer-to-peer networking, and a web-based blockchain explorer.

---

# 📌 Features

* 🔐 Secure Wallet Generation (ECC - secp256k1)
* 💰 Cryptocurrency Transactions
* ⛏️ Proof-of-Work Mining
* 🔗 Blockchain Explorer
* 📦 Mempool (Pending Transactions)
* 🌳 Merkle Tree Generation
* 🔒 SHA-256 Cryptographic Hashing
* 💳 UTXO Transaction Model
* 🌐 Peer-to-Peer (P2P) Networking
* 📊 Mining Dashboard
* 📜 Transaction History
* 🎨 Modern Flask Web Interface

---

# 🛠️ Technologies Used

## Programming Language

* Python

## Backend Framework

* Flask

## Frontend

* HTML
* CSS
* JavaScript

## Cryptography

* SHA-256
* secp256k1 Elliptic Curve Cryptography

## Networking

* Python Socket Programming
* ngrok (Remote Access)

## Storage

* JSON-based Blockchain Storage

---

# 📁 Project Structure

```
BitChain-Network/
│
├── Blockchain/
│   │
│   ├── Backend/
│   │   │
│   │   ├── Core/
│   │   │   ├── database/
│   │   │   └── EllipticCurve/
│   │   │
│   │   └── util/
│   │
│   ├── client/
│   │
│   ├── Frontend/
│   │   │
│   │   ├── static/
│   │   │   ├── css/
│   │   │   └── js/
│   │   │
│   │   ├── templates/
│   │       
│   │
│   └── run.py
|       # Main Flask application entry point
│
├── data/
│   └── .gitkeep
│   # Runtime JSON files (account.json, blockchain.json,
│   # pending_txn.json) are created automatically.
│
├── .env.example
├── .gitignore
├── requirements.txt
├── README.md
└── LICENSE
```

---

# ⚙️ Installation

## 1. Clone the Repository

```bash
git clone https://github.com/YOUR_USERNAME/BitChain-Network.git

cd BitChain-Network
```

---

## 2. Create Virtual Environment

### Windows

```bash
python -m venv venv
```

Activate

```bash
venv\Scripts\activate
```

### Linux / macOS

```bash
python3 -m venv venv

source venv/bin/activate
```

---

## 3. Install Required Packages

```bash
pip install -r requirements.txt
```

---

## 4. Configure Environment Variables

Create a file named

```
.env
```

Add the following variables:

```env
BITCHAIN_EMAIL=your_email@gmail.com
BITCHAIN_PASSWORD=your_16_character_gmail_app_password

SECRET_KEY=your_random_secret_key
```

### Environment Variables

#### BITCHAIN_EMAIL

Gmail account used for sending OTP verification emails.

#### BITCHAIN_PASSWORD

16-character Gmail App Password associated with the above Gmail account.

#### SECRET_KEY

Random Flask secret key used for securing sessions and application security.

---

## 5. Run the Application

```bash
python run.py
```

Open your browser and visit

```
http://127.0.0.1:5000
```

---

# 💡 How the Project Works

## 1. User Registration

* User registers an account.
* A unique blockchain wallet is generated.
* Public and private key pairs are created using secp256k1.
* OTP verification is performed through Gmail SMTP.

---

## 2. Wallet Generation

Each wallet contains

* Wallet Address
* Public Key
* Private Key
* Balance
* Transaction History

Wallet information is securely stored.

---

## 3. Sending Bitcoin

A user enters

* Receiver Address
* Amount
* Transaction Fee

The system verifies

* Wallet balance
* Available UTXOs
* Transaction validity

A transaction is created and placed inside the Mempool.

---

## 4. Mempool

The Mempool temporarily stores all valid but unconfirmed transactions.

Transactions remain here until a miner successfully mines a new block.

---

## 5. Mining

Mining follows the Proof-of-Work consensus algorithm.

The miner

* Collects pending transactions
* Calculates the Merkle Root
* Searches for a valid Nonce
* Generates a SHA-256 hash satisfying the mining difficulty

Once successful

* A new block is created.
* Mining reward is credited.
* Pending transactions become confirmed.

---

## 6. Blockchain

Each block contains

* Previous Hash
* Current Hash
* Nonce
* Timestamp
* Transactions
* Merkle Root

Every block references the previous block hash, creating an immutable blockchain.

---

## 7. Transaction Verification

Before confirmation every transaction is verified using

* Digital Signature
* UTXO Validation
* Wallet Balance
* Transaction Integrity

Only valid transactions are added into blocks.

---

## 8. Blockchain Explorer

Users can explore

* Latest Blocks
* Transaction History
* Block Details
* Wallet Information
* Blockchain Statistics

---

## 9. P2P Network

The system supports Peer-to-Peer communication.

Connected peers can

* Share blockchain data
* Broadcast transactions
* Synchronize newly mined blocks

No central authority controls communication.

---

## 10. Consensus

BitChain Network implements

**Proof-of-Work (PoW)**

Miners compete to solve computational puzzles.

The first miner finding a valid hash earns

* Block Reward
* Transaction Fees

The verified block is appended to the blockchain.

---

# 🔒 Security Features

* SHA-256 Hashing
* Elliptic Curve Cryptography (secp256k1)
* Digital Signatures
* UTXO Validation
* Proof-of-Work Consensus
* Immutable Blockchain Structure

---

# 📸 Application Screenshots

Add screenshots here, for example:

* Home Dashboard
* Wallet Dashboard
* Blockchain Explorer
* Transactions
* Mempool
* Mining Interface
* P2P Network

---

# 🚀 Future Enhancements

* Smart Contracts
* Multi-node Distributed Blockchain
* REST API Support
* Docker Deployment
* MongoDB / PostgreSQL Integration
* Dynamic Mining Difficulty
* Mobile Wallet
* Real-Time Blockchain Synchronization

---

# 👨‍💻 Author

**Ronak Raj**

B.Tech Computer Science Engineering

Blockchain | Python | Flask | Cybersecurity | Backend Development

---

# 📄 License

This project is licensed under the MIT License.
