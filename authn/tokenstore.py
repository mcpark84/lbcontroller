"""PoC token store shared by token_api.py (issue / list / revoke) and
webhook_server.py (verify). sqlite, standard library only.

Token format : "pt_" + base64url(32 random bytes)  -> 256 bits of entropy
Stored value : SHA-256 hex of the plaintext. Plaintext is never written.
               (NIST SP 800-63B: look-up secrets with >=112 bits of entropy
               only need an approved one-way hash; no salt / slow KDF.)
Revocation   : rows are never deleted; revoked_at is set (audit trail).
"""
import base64
import datetime as _dt
import hashlib
import secrets
import sqlite3
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
  id           TEXT PRIMARY KEY,
  hash         TEXT NOT NULL UNIQUE,
  owner_id     TEXT NOT NULL,
  tenant       TEXT NOT NULL,
  cluster      TEXT NOT NULL,
  issued_via   TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  expires_at   TEXT,
  revoked_at   TEXT,
  last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS tokens_owner ON tokens(owner_id);
"""
PUBLIC_COLS = ("id", "owner_id", "tenant", "cluster", "issued_via",
               "created_at", "expires_at", "revoked_at", "last_used_at")
PREFIX = "pt_"


def now():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s):
    return _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)


def open_db(path):
    conn = sqlite3.connect(path, timeout=2.0, isolation_level=None,  # autocommit
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=2000")
    conn.executescript(SCHEMA)
    return conn


def new_token():
    return PREFIX + base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def hash_token(plaintext):
    return hashlib.sha256(plaintext.encode()).hexdigest()


def hash_prefix(plaintext, n=12):
    """What goes into logs: never the plaintext."""
    return hash_token(plaintext)[:n]


def public(row):
    d = {k: row[k] for k in PUBLIC_COLS}
    d["hash_prefix"] = row["hash"][:12]
    return d


def issue(conn, owner_id, tenant, cluster, issued_via, ttl_seconds):
    plaintext = new_token()
    t = now()
    row = {
        "id": uuid.uuid4().hex[:12],
        "hash": hash_token(plaintext),
        "owner_id": owner_id, "tenant": tenant, "cluster": cluster,
        "issued_via": issued_via,
        "created_at": iso(t),
        "expires_at": iso(t + _dt.timedelta(seconds=int(ttl_seconds))) if ttl_seconds else None,
        "revoked_at": None, "last_used_at": None,
    }
    conn.execute("INSERT INTO tokens VALUES (:id,:hash,:owner_id,:tenant,:cluster,:issued_via,"
                 ":created_at,:expires_at,:revoked_at,:last_used_at)", row)
    return plaintext, row


def list_tokens(conn, owner=None):
    if owner:
        cur = conn.execute("SELECT * FROM tokens WHERE owner_id=? ORDER BY created_at", (owner,))
    else:
        cur = conn.execute("SELECT * FROM tokens ORDER BY created_at")
    return [public(r) for r in cur.fetchall()]


def get(conn, token_id):
    r = conn.execute("SELECT * FROM tokens WHERE id=?", (token_id,)).fetchone()
    return public(r) if r else None


def revoke(conn, token_id):
    cur = conn.execute("UPDATE tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                       (iso(now()), token_id))
    return cur.rowcount


def revoke_user(conn, owner_id):
    cur = conn.execute("UPDATE tokens SET revoked_at=? WHERE owner_id=? AND revoked_at IS NULL",
                       (iso(now()), owner_id))
    return cur.rowcount


def verify(conn, plaintext, cluster_id):
    """Returns (ok, reason, row). Order follows the design doc:
    lookup -> revoked -> expired -> cluster mismatch -> touch last_used_at."""
    if not plaintext:
        return False, "empty", None
    r = conn.execute("SELECT * FROM tokens WHERE hash=?", (hash_token(plaintext),)).fetchone()
    if r is None:
        return False, "unknown", None
    if r["revoked_at"]:
        return False, "revoked", r
    if r["expires_at"] and parse(r["expires_at"]) <= now():
        return False, "expired", r
    if r["cluster"] != cluster_id:
        return False, "cluster-mismatch", r
    try:
        conn.execute("UPDATE tokens SET last_used_at=? WHERE id=?", (iso(now()), r["id"]))
    except sqlite3.OperationalError:
        pass  # availability over bookkeeping: a locked DB must not fail authn
    return True, None, r


def identity(row):
    """Fixed mapping for phase 2 (IdP integration is phase 3)."""
    return {"username": row["owner_id"],
            "uid": row["owner_id"],
            "groups": ["poc-tenant-" + row["tenant"]]}
