"""db.py — Banco SQLite do BRN (v6)
v4: + delete_blocks_above, + get_blocks_range, + peer score
v5: + get_next_nonce, + get_nonce_for_pubkey (protecao replay)
v6: + L2 tables (l2_escrows, btc_used, l2_blocks, l2_txs) + BTC->BRN escrow
"""
import time
import zlib
import sqlite3
import threading
import orjson
import json

class ChainDB:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        for p in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL",
                  "PRAGMA temp_store=MEMORY", "PRAGMA cache_size=-20000",
                  "PRAGMA mmap_size=268435456", "PRAGMA foreign_keys=ON"):
            self.conn.execute(p)
        self._schema()

    def _schema(self):
        with self.lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS blocks (
                height INTEGER PRIMARY KEY, hash TEXT UNIQUE NOT NULL,
                prev_hash TEXT NOT NULL, timestamp INTEGER NOT NULL,
                nonce INTEGER NOT NULL, merkle TEXT NOT NULL,
                difficulty INTEGER NOT NULL, raw BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_blocks_hash ON blocks(hash);
            CREATE INDEX IF NOT EXISTS idx_blocks_prev ON blocks(prev_hash);
            CREATE TABLE IF NOT EXISTS transactions (
                txid TEXT PRIMARY KEY, block_height INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tx_block ON transactions(block_height);
            CREATE TABLE IF NOT EXISTS utxos (
                txid TEXT NOT NULL, vout INTEGER NOT NULL,
                address TEXT NOT NULL, amount INTEGER NOT NULL,
                pubkey TEXT NOT NULL, block_height INTEGER NOT NULL,
                spent INTEGER NOT NULL DEFAULT 0, spent_by TEXT,
                PRIMARY KEY (txid, vout)
            );
            CREATE INDEX IF NOT EXISTS idx_utxo_addr ON utxos(address, spent);
            CREATE INDEX IF NOT EXISTS idx_utxo_spent ON utxos(spent);
            CREATE INDEX IF NOT EXISTS idx_utxo_h ON utxos(block_height);
            CREATE TABLE IF NOT EXISTS mempool (
                txid TEXT PRIMARY KEY, raw BLOB NOT NULL,
                fee INTEGER NOT NULL, received_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_mp_fee ON mempool(fee DESC);
            CREATE INDEX IF NOT EXISTS idx_mp_time ON mempool(received_at);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS peers (
                address TEXT PRIMARY KEY NOT NULL, node_id TEXT NOT NULL,
                genesis_hash TEXT NOT NULL, version TEXT,
                height INTEGER DEFAULT 0, is_miner INTEGER DEFAULT 0,
                public_key TEXT, metadata TEXT,
                first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
                score INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_peers_last_seen ON peers(last_seen);
            CREATE INDEX IF NOT EXISTS idx_peers_genesis ON peers(genesis_hash);
            CREATE INDEX IF NOT EXISTS idx_peers_node_id ON peers(node_id);
            CREATE TABLE IF NOT EXISTS network_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL, event_type TEXT NOT NULL,
                peer_address TEXT, details TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_events_ts ON network_events(timestamp DESC);
            
            -- v6 L2 TABLES --
            CREATE TABLE IF NOT EXISTS l2_escrows (
                escrow_id TEXT PRIMARY KEY,
                creator TEXT,
                buyer TEXT,
                brn_amount INTEGER,
                btc_expected_sats INTEGER,
                btc_address TEXT,
                btc_txid TEXT,
                status TEXT,
                created_at INTEGER,
                expires_at INTEGER,
                l2_hash TEXT,
                raw_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_l2_status ON l2_escrows(status);
            CREATE INDEX IF NOT EXISTS idx_l2_buyer ON l2_escrows(buyer);
            
            CREATE TABLE IF NOT EXISTS btc_used (
                btc_txid TEXT PRIMARY KEY,
                escrow_id TEXT,
                used_at INTEGER
            );
            
            CREATE TABLE IF NOT EXISTS l2_blocks (
                merkle_root TEXT PRIMARY KEY,
                timestamp INTEGER,
                count INTEGER,
                raw_json TEXT
            );
            
            CREATE TABLE IF NOT EXISTS l2_txs (
                txid TEXT PRIMARY KEY,
                type TEXT,
                escrow_id TEXT,
                raw_json TEXT,
                timestamp INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_l2tx_escrow ON l2_txs(escrow_id);
            """)
            try:
                cols = self.conn.execute("PRAGMA table_info(peers)").fetchall()
                if "score" not in {c["name"] for c in cols}:
                    self.conn.execute("ALTER TABLE peers ADD COLUMN score INTEGER DEFAULT 0")
            except Exception:
                pass

    # ==================== BLOCOS ====================
    def add_block(self, block):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE TRANSACTION")
            try:
                blob = zlib.compress(orjson.dumps(block), level=6)
                self.conn.execute(
                    "INSERT INTO blocks(height,hash,prev_hash,timestamp,nonce,merkle,difficulty,raw) VALUES (?,?,?,?,?,?,?,?)",
                    (block["height"], block["hash"], block["prev_hash"], block["timestamp"],
                     block["nonce"], block["merkle"], block["difficulty"], blob))
                for tx in block["transactions"]:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO transactions(txid,block_height) VALUES (?,?)",
                        (tx["txid"], block["height"]))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def delete_blocks_above(self, height):
        with self.lock:
            n = self.conn.execute("DELETE FROM blocks WHERE height > ?", (height,)).rowcount
            self.conn.execute("DELETE FROM transactions WHERE block_height > ?", (height,))
            self.conn.execute("DELETE FROM utxos WHERE block_height > ?", (height,))
            return n

    def get_block(self, height):
        row = self.conn.execute("SELECT raw FROM blocks WHERE height=?", (height,)).fetchone()
        return orjson.loads(zlib.decompress(row["raw"])) if row else None

    def get_block_by_hash(self, h):
        row = self.conn.execute("SELECT raw FROM blocks WHERE hash=?", (h,)).fetchone()
        return orjson.loads(zlib.decompress(row["raw"])) if row else None

    def get_blocks_range(self, start, end):
        rows = self.conn.execute(
            "SELECT raw FROM blocks WHERE height >= ? AND height < ? ORDER BY height",
            (start, end)).fetchall()
        return [orjson.loads(zlib.decompress(r["raw"])) for r in rows]

    def height(self):
        row = self.conn.execute("SELECT MAX(height) AS h FROM blocks").fetchone()
        return int(row["h"]) if row and row["h"] is not None else -1

    def tip(self):
        row = self.conn.execute("SELECT hash,prev_hash FROM blocks ORDER BY height DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def tip_hash(self):
        t = self.tip()
        return t["hash"] if t else "0" * 64

    def headers(self, start, limit=2000):
        rows = self.conn.execute(
            "SELECT height,hash,prev_hash,timestamp,nonce,merkle,difficulty FROM blocks WHERE height>=? ORDER BY height LIMIT ?",
            (start, limit)).fetchall()
        return [dict(r) for r in rows]

    def latest_blocks(self, n=10):
        top = self.height()
        if top < 0:
            return []
        return [b for b in (self.get_block(h) for h in range(max(0, top - n + 1), top + 1)) if b]

    # ==================== UTXO ====================
    def balance(self, address):
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM utxos WHERE address=? AND spent=0",
            (address,)).fetchone()
        return int(row["s"])

    def utxos_for(self, address):
        rows = self.conn.execute(
            "SELECT txid,vout,amount,pubkey FROM utxos WHERE address=? AND spent=0 ORDER BY amount DESC",
            (address,)).fetchall()
        return [dict(r) for r in rows]

    def get_utxos(self, address):
        return self.utxos_for(address)

    def get_utxo(self, txid, vout):
        row = self.conn.execute(
            "SELECT * FROM utxos WHERE txid=? AND vout=? AND spent=0", (txid, vout)).fetchone()
        return dict(row) if row else None

    def apply_tx(self, tx, height, coinbase=False):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE TRANSACTION")
            try:
                if not coinbase:
                    for inp in tx["inputs"]:
                        self.conn.execute(
                            "UPDATE utxos SET spent=1, spent_by=? WHERE txid=? AND vout=? AND spent=0",
                            (tx["txid"], inp["txid"], inp["vout"]))
                for i, out in enumerate(tx["outputs"]):
                    self.conn.execute(
                        "INSERT OR REPLACE INTO utxos(txid,vout,address,amount,pubkey,block_height,spent) VALUES (?,?,?,?,?,?,0)",
                        (tx["txid"], i, out["address"], out["amount"], out.get("pubkey", ""), height))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def rollback_block(self, height):
        with self.lock:
            block = self.get_block(height)
            if not block:
                return
            self.conn.execute("BEGIN IMMEDIATE TRANSACTION")
            try:
                for tx in reversed(block["transactions"]):
                    for i in range(len(tx["outputs"])):
                        self.conn.execute("DELETE FROM utxos WHERE txid=? AND vout=?", (tx["txid"], i))
                    if height > 0:
                        for inp in tx.get("inputs", []):
                            if inp["txid"] == "0" * 64:
                                continue
                            self.conn.execute("UPDATE utxos SET spent=0, spent_by=NULL WHERE txid=? AND vout=?",
                                              (inp["txid"], inp["vout"]))
                    self.conn.execute("DELETE FROM transactions WHERE txid=?", (tx["txid"],))
                self.conn.execute("DELETE FROM blocks WHERE height=?", (height,))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    # ==================== NONCE / REPLAY PROTECTION (v5) ====================
    def get_next_nonce(self, address: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM utxos WHERE address=? AND spent=1",
            (address,)
        ).fetchone()
        return int(row["c"])

    def get_nonce_for_pubkey(self, pubkey: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM utxos WHERE pubkey=? AND spent=1",
            (pubkey,)
        ).fetchone()
        return int(row["c"])

    # ==================== MEMPOOL ====================
    def add_mempool(self, tx, fee):
        with self.lock:
            try:
                self.conn.execute("INSERT INTO mempool(txid,raw,fee,received_at) VALUES (?,?,?,?)",
                                  (tx["txid"], zlib.compress(orjson.dumps(tx)), fee, time.time()))
            except sqlite3.IntegrityError:
                return False
            self._prune_mempool()
            return True

    def _prune_mempool(self, max_size=1000, ttl=3600):
        self.conn.execute("DELETE FROM mempool WHERE received_at < ?", (time.time() - ttl,))
        n = self.conn.execute("SELECT COUNT(*) AS c FROM mempool").fetchone()["c"]
        if n > max_size:
            self.conn.execute(
                "DELETE FROM mempool WHERE txid IN (SELECT txid FROM mempool ORDER BY fee ASC, received_at ASC LIMIT ?)",
                (n - max_size,))

    def has_mempool(self, txid):
        return self.conn.execute("SELECT 1 FROM mempool WHERE txid=?", (txid,)).fetchone() is not None

    def get_mempool_tx(self, txid):
        row = self.conn.execute("SELECT raw FROM mempool WHERE txid=?", (txid,)).fetchone()
        return orjson.loads(zlib.decompress(row["raw"])) if row else None

    def all_mempool(self, limit=1000):
        rows = self.conn.execute("SELECT raw FROM mempool ORDER BY fee DESC LIMIT ?", (limit,)).fetchall()
        return [orjson.loads(zlib.decompress(r["raw"])) for r in rows]

    def remove_mempool(self, txid):
        with self.lock:
            self.conn.execute("DELETE FROM mempool WHERE txid=?", (txid,))

    def mempool_count(self):
        return self.conn.execute("SELECT COUNT(*) AS c FROM mempool").fetchone()["c"]

    def mempool_stats(self):
        row = self.conn.execute("SELECT COUNT(*) AS c, COALESCE(SUM(fee),0) AS sf FROM mempool").fetchone()
        fees = self.conn.execute("SELECT fee FROM mempool ORDER BY fee DESC LIMIT 100").fetchall()
        return {"count": row["c"], "sum_fee": row["sf"], "fees": [f["fee"] for f in fees]}

    # ==================== L2 - BTC->BRN (v6) ====================
    def save_l2_escrow(self, escrow_dict):
        with self.lock:
            self.conn.execute("""
                INSERT OR REPLACE INTO l2_escrows 
                (escrow_id, creator, buyer, brn_amount, btc_expected_sats, btc_address, btc_txid, status, created_at, expires_at, l2_hash, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                escrow_dict['escrow_id'],
                escrow_dict['creator'],
                escrow_dict['buyer'],
                escrow_dict['brn_amount'],
                escrow_dict['btc_expected_sats'],
                escrow_dict['btc_address'],
                escrow_dict.get('btc_txid'),
                escrow_dict['status'],
                escrow_dict['created_at'],
                escrow_dict['expires_at'],
                escrow_dict['l2_hash'],
                json.dumps(escrow_dict)
            ))

    def get_l2_escrows(self, status=None):
        if status:
            rows = self.conn.execute("SELECT raw_json FROM l2_escrows WHERE status=? ORDER BY created_at DESC", (status,)).fetchall()
        else:
            rows = self.conn.execute("SELECT raw_json FROM l2_escrows ORDER BY created_at DESC").fetchall()
        result = []
        for r in rows:
            try:
                result.append(json.loads(r["raw_json"]))
            except:
                pass
        return result

    def get_l2_escrow_by_id(self, escrow_id):
        row = self.conn.execute("SELECT raw_json FROM l2_escrows WHERE escrow_id=?", (escrow_id,)).fetchone()
        if row:
            try:
                return json.loads(row["raw_json"])
            except:
                return None
        return None

    def update_l2_escrow_status(self, escrow_id, status, btc_txid=None):
        with self.lock:
            escrow = self.get_l2_escrow_by_id(escrow_id)
            if not escrow:
                return
            escrow['status'] = status
            if btc_txid:
                escrow['btc_txid'] = btc_txid
            self.conn.execute("UPDATE l2_escrows SET status=?, btc_txid=?, raw_json=? WHERE escrow_id=?",
                              (status, escrow.get('btc_txid'), json.dumps(escrow), escrow_id))

    def is_btc_txid_used(self, btc_txid):
        row = self.conn.execute("SELECT 1 FROM btc_used WHERE btc_txid=?", (btc_txid,)).fetchone()
        return row is not None

    def mark_btc_txid_used(self, btc_txid, escrow_id):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO btc_used (btc_txid, escrow_id, used_at) VALUES (?, ?, ?)",
                              (btc_txid, escrow_id, int(time.time())))

    def save_l2_block(self, l2_block):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO l2_blocks (merkle_root, timestamp, count, raw_json) VALUES (?, ?, ?, ?)",
                              (l2_block['merkle_root'], l2_block['timestamp'], l2_block.get('count', 1), json.dumps(l2_block)))

    def save_l2_tx(self, l2_tx):
        import hashlib
        with self.lock:
            txid = hashlib.sha256(json.dumps(l2_tx).encode()).hexdigest()[:16]
            self.conn.execute("INSERT OR REPLACE INTO l2_txs (txid, type, escrow_id, raw_json, timestamp) VALUES (?, ?, ?, ?, ?)",
                              (txid, l2_tx['type'], l2_tx.get('escrow_id'), json.dumps(l2_tx), int(time.time())))
            return txid

    def get_l2_txs(self, limit=50):
        rows = self.conn.execute("SELECT raw_json FROM l2_txs ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r["raw_json"]) for r in rows]

    # ==================== ESTATISTICAS ====================
    def get_stats(self):
        return {"height": self.height(), "tip_hash": self.tip_hash(),
                "utxos": self.count_utxos(), "mempool": self.mempool_count(),
                "blocks": self.height() + 1,
                "l2_escrows": self.conn.execute("SELECT COUNT(*) as c FROM l2_escrows").fetchone()["c"],
                "l2_txs": self.conn.execute("SELECT COUNT(*) as c FROM l2_txs").fetchone()["c"]}

    def prune_spent_utxos(self, keep_height=1000):
        cutoff = max(0, self.height() - keep_height)
        with self.lock:
            return self.conn.execute("DELETE FROM utxos WHERE spent=1 AND block_height < ?", (cutoff,)).rowcount

    def count_utxos(self):
        return self.conn.execute("SELECT COUNT(*) AS c FROM utxos WHERE spent=0").fetchone()["c"]

    def vacuum(self):
        with self.lock:
            self.conn.execute("VACUUM")

    def set_meta(self, key, value):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", (key, value))

    def get_meta(self, key):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def upsert_peer(self, node_id, address, genesis_hash, version="?", height=0,
                    is_miner=False, public_key="", metadata=None):
        with self.lock:
            agora = int(time.time())
            existente = self.conn.execute("SELECT node_id FROM peers WHERE address=?", (address,)).fetchone()
            novo = existente is None
            self.conn.execute("""
                INSERT INTO peers (address, node_id, genesis_hash, version, height,
                                   is_miner, public_key, metadata, first_seen, last_seen, score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(address) DO UPDATE SET
                    node_id=excluded.node_id, genesis_hash=excluded.genesis_hash,
                    version=excluded.version, height=excluded.height,
                    is_miner=excluded.is_miner, public_key=excluded.public_key,
                    metadata=excluded.metadata, last_seen=excluded.last_seen
            """, (address, node_id, genesis_hash, version, height,
                  1 if is_miner else 0, public_key,
                  orjson.dumps(metadata or {}).decode(), agora, agora))
            if novo:
                self._log_event("peer_joined", address, f"node_id={node_id[:12]}")
            return novo

    def get_peer_score(self, address):
        row = self.conn.execute("SELECT score FROM peers WHERE address=?", (address,)).fetchone()
        return int(row["score"]) if row else 0

    def set_peer_score(self, address, score):
        with self.lock:
            self.conn.execute("UPDATE peers SET score=? WHERE address=?", (score, address))

    def add_peer_score(self, address, delta):
        with self.lock:
            self.conn.execute("UPDATE peers SET score = COALESCE(score, 0) + ? WHERE address=?", (delta, address))
            return self.get_peer_score(address)

    def listar_peers(self, apenas_ativos=True, janela=300):
        if apenas_ativos:
            cutoff = int(time.time()) - janela
            rows = self.conn.execute("SELECT * FROM peers WHERE last_seen >= ? ORDER BY last_seen DESC", (cutoff,)).fetchall()
        else:
            rows = self.conn.execute("SELECT *