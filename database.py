"""SQLite storage for local simulator orders and independent run history."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import sqlite3
from sensitive_data import assert_protected_order, SAFE_STAGES

DB_PATH = Path(os.environ.get('ALLCONNECT_DB', Path(__file__).parent / 'allconnect_secure.db'))
TERMINAL = {'installation_completed', 'failed', 'cancelled'}
ERROR_CODES = {'automation_error', 'api_unavailable', 'interrupted', 'startup_error', 'manual_recovery'}

class Conflict(ValueError):
    pass

class Missing(ValueError):
    pass

def now():
    return datetime.now(timezone.utc).isoformat()

@contextmanager
def connection():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    try:
        with con:
            yield con
    finally:
        con.close()

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connection() as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.executescript('''
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL REFERENCES orders(id),
            status TEXT NOT NULL CHECK(status IN ('running','installation_completed','failed','cancelled')),
            stage TEXT NOT NULL, error_code TEXT,
            started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            finished_at TEXT, order_snapshot TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_order
            ON runs(order_id) WHERE status='running';
        ''')

def import_orders(orders, update=False):
    added = skipped = updated = 0
    with connection() as con:
        con.execute('BEGIN IMMEDIATE')
        for key, payload in orders.items():
            assert_protected_order(payload, key)
            existing = con.execute('SELECT id FROM orders WHERE id=?', (key,)).fetchone()
            stamp = now()
            if existing:
                if not update:
                    skipped += 1
                    continue
                if con.execute("SELECT id FROM runs WHERE order_id=? AND status='running'", (key,)).fetchone():
                    raise Conflict('Cannot update an order while its automation run is active.')
                con.execute('UPDATE orders SET payload=?,updated_at=? WHERE id=?',
                            (json.dumps(payload), stamp, key))
                updated += 1
            else:
                con.execute('INSERT INTO orders VALUES (?,?,?,?)',
                            (key, json.dumps(payload), stamp, stamp))
                added += 1
    return {'added': added, 'updated': updated, 'skipped': skipped}

def get_order(key):
    with connection() as con:
        row = con.execute('SELECT * FROM orders WHERE id=?', (key,)).fetchone()
    if row is None:
        raise Missing('Order not found.')
    payload = json.loads(row['payload'])
    assert_protected_order(payload, key)
    return {'id': row['id'], 'data': payload, 'updated_at': row['updated_at']}

def list_orders(limit=100, offset=0):
    with connection() as con:
        rows = con.execute('SELECT id,updated_at FROM orders ORDER BY id LIMIT ? OFFSET ?', (limit,offset)).fetchall()
    return [dict(row) for row in rows]

def public_run(row, snapshot=False):
    item = dict(row)
    payload = item.pop('order_snapshot')
    if snapshot:
        protected = json.loads(payload)
        assert_protected_order(protected, row['order_id'])
        item['order'] = protected
    item['final_order_submitted'] = False
    return item

def start_run(key, run_id):
    with connection() as con:
        con.execute('BEGIN IMMEDIATE')
        previous = con.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if previous:
            if previous['order_id'] != key:
                raise Conflict('Run ID already belongs to another order.')
            return public_run(previous, snapshot=True)
        order = con.execute('SELECT payload FROM orders WHERE id=?', (key,)).fetchone()
        if order is None:
            raise Missing('Order not found.')
        assert_protected_order(json.loads(order['payload']), key)
        if con.execute("SELECT id FROM runs WHERE order_id=? AND status='running'", (key,)).fetchone():
            raise Conflict('An active run already exists. Check GET /runs before retrying.')
        stamp = now()
        con.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)',
                    (run_id,key,'running','opening simulator',None,stamp,stamp,None,order['payload']))
        row = con.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
    return public_run(row, snapshot=True)

def update_run(run_id, status, stage, error_code=None):
    if stage not in SAFE_STAGES:
        raise ValueError('Use a predefined workflow stage without customer values.')
    if status not in TERMINAL | {'running'}:
        raise ValueError('Invalid run status.')
    if error_code is not None and error_code not in ERROR_CODES:
        raise ValueError('Invalid error code.')
    with connection() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise Missing('Run not found.')
        if row['status'] != 'running':
            if row['status'] == status and row['stage'] == stage and row['error_code'] == error_code:
                return public_run(row)
            raise Conflict('A finished run cannot be changed or restarted.')
        stamp = now()
        con.execute('UPDATE runs SET status=?,stage=?,error_code=?,updated_at=?,finished_at=? WHERE id=?',
                    (status,stage,error_code,stamp,stamp if status in TERMINAL else None,run_id))
        row = con.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
    return public_run(row)

def list_runs(limit=100, offset=0):
    with connection() as con:
        rows = con.execute('SELECT * FROM runs ORDER BY started_at DESC LIMIT ? OFFSET ?', (limit,offset)).fetchall()
    return [public_run(row) for row in rows]

def get_run(run_id):
    with connection() as con:
        row = con.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
    if row is None:
        raise Missing('Run not found.')
    return public_run(row)


def check_stored_protection():
    """Refuse to serve a legacy database containing unprotected sensitive fields."""
    with connection() as con:
        for row in con.execute('SELECT id,payload FROM orders'):
            assert_protected_order(json.loads(row['payload']), row['id'])
        for row in con.execute('SELECT order_id,order_snapshot,stage FROM runs'):
            assert_protected_order(json.loads(row['order_snapshot']), row['order_id'])
            if row['stage'] not in SAFE_STAGES:
                raise ValueError('A legacy run needs migration to safe stage metadata.')
