"""SQLite storage for local simulator orders and independent run history."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import json
import sqlite3
from sensitive_data import assert_non_sensitive_order, SAFE_STAGES, SENSITIVE_ORDER_KEYS

DB_PATH = (Path(__file__).resolve().parent / 'allconnect_demo.db').resolve()
TERMINAL = {'installation_completed', 'failed', 'cancelled'}
ERROR_CODES = {'automation_error', 'api_unavailable', 'interrupted', 'startup_error', 'manual_recovery'}
STALE_RUN_AFTER_SECONDS = 15 * 60
WORKER_HEARTBEAT_SECONDS = 10
WORKER_HEARTBEAT_TIMEOUT_SECONDS = 45

class Conflict(ValueError):
    pass

class Missing(ValueError):
    pass

def now():
    return datetime.now(timezone.utc).isoformat()

@contextmanager
def connection():
    # Pin API reads and writes to the project's declared database so an
    # inherited ALLCONNECT_DB setting cannot silently select a stale copy.
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
            finished_at TEXT, order_snapshot TEXT NOT NULL,
            final_order_submitted INTEGER NOT NULL DEFAULT 0,
            submission_state TEXT NOT NULL DEFAULT 'not_attempted'
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_order
            ON runs(order_id) WHERE status='running';
        CREATE TABLE IF NOT EXISTS sensitive_replay (
            jti_hash TEXT PRIMARY KEY,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS run_workers (
            run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
            worker_id TEXT NOT NULL,
            worker_pid INTEGER NOT NULL,
            heartbeat_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
        );
        ''')
        # Safe additive migration for databases created before submission tracking.
        columns = {row['name'] for row in con.execute('PRAGMA table_info(runs)')}
        if 'final_order_submitted' not in columns:
            con.execute(
                'ALTER TABLE runs ADD COLUMN final_order_submitted INTEGER NOT NULL DEFAULT 0'
            )
        if 'submission_state' not in columns:
            con.execute(
                "ALTER TABLE runs ADD COLUMN submission_state TEXT NOT NULL DEFAULT 'not_attempted'"
            )

def import_orders(orders, update=False):
    added = skipped = updated = 0
    with connection() as con:
        con.execute('BEGIN IMMEDIATE')
        for key, payload in orders.items():
            assert_non_sensitive_order(payload, key)
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
    assert_non_sensitive_order(payload, key)
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
        assert_non_sensitive_order(protected, row['order_id'])
        item['order'] = protected
    item['final_order_submitted'] = bool(item.get('final_order_submitted', 0))
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
        assert_non_sensitive_order(json.loads(order['payload']), key)
        if con.execute("SELECT id FROM runs WHERE order_id=? AND status='running'", (key,)).fetchone():
            raise Conflict('An active run already exists. Check GET /runs before retrying.')
        if con.execute(
            '''SELECT id FROM runs WHERE order_id=?
               AND (final_order_submitted=1 OR status='installation_completed'
                    OR submission_state IN ('attempting','ambiguous','unknown')) LIMIT 1''',
            (key,),
        ).fetchone():
            raise Conflict('This order has a confirmed submission and cannot be run again.')
        stamp = now()
        con.execute('''INSERT INTO runs (
            id,order_id,status,stage,error_code,started_at,updated_at,finished_at,
            order_snapshot,final_order_submitted,submission_state
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                    (run_id,key,'running','opening simulator',None,stamp,stamp,None,
                     order['payload'],0,'not_attempted'))
        row = con.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
    return public_run(row)

def set_submission_state(run_id, state):
    allowed = {'not_attempted', 'attempting', 'ambiguous', 'confirmed', 'not_submitted', 'unknown'}
    if state not in allowed:
        raise ValueError('Invalid submission state.')
    with connection() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise Missing('Run not found.')
        old = row['submission_state'] if 'submission_state' in row.keys() else 'not_attempted'
        if old == state:
            return public_run(row)
        transitions = {
            'not_attempted': {'attempting'},
            'attempting': {'confirmed', 'ambiguous', 'unknown'},
            'ambiguous': {'confirmed', 'not_submitted', 'unknown'},
            'confirmed': set(),
            'not_submitted': set(),
        }
        if row['status'] != 'running' or state not in transitions.get(old, set()):
            raise Conflict('Submission state cannot be changed from its current state.')
        submitted = int(state == 'confirmed')
        con.execute(
            'UPDATE runs SET submission_state=?, final_order_submitted=? WHERE id=?',
            (state, submitted, run_id),
        )
        row = con.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
    return public_run(row)

def resolve_ambiguous_submission(run_id, *, submitted):
    """Resolve only after manual simulator verification; never retries submission."""
    if not isinstance(submitted, bool):
        raise ValueError('Provide a verified boolean submission outcome.')
    state = 'confirmed' if submitted else 'not_submitted'
    with connection() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise Missing('Run not found.')
        if row['status'] != 'running' or row['submission_state'] not in (
            'attempting', 'ambiguous'
        ):
            raise Conflict('Run is not awaiting submission recovery.')
        stamp = now()
        if submitted:
            con.execute('''UPDATE runs SET submission_state='confirmed', final_order_submitted=1,
                status='installation_completed', stage='order submitted', error_code=NULL,
                updated_at=?, finished_at=? WHERE id=?''', (stamp, stamp, run_id))
        else:
            con.execute('''UPDATE runs SET submission_state='not_submitted', final_order_submitted=0,
                status='failed', stage='manual recovery', error_code='manual_recovery',
                updated_at=?, finished_at=? WHERE id=?''', (stamp, stamp, run_id))
        row = con.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
    return public_run(row)

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
        if status == 'installation_completed' and (
            row['submission_state'] != 'confirmed'
            or not bool(row['final_order_submitted'])
        ):
            raise Conflict('Completion requires persisted simulator submission confirmation.')
        if row['status'] != 'running':
            if row['status'] == status and row['stage'] == stage and row['error_code'] == error_code:
                return public_run(row)
            raise Conflict('A finished run cannot be changed or restarted.')
        stamp = now()
        con.execute('UPDATE runs SET status=?,stage=?,error_code=?,updated_at=?,finished_at=? WHERE id=?',
                    (status,stage,error_code,stamp,stamp if status in TERMINAL else None,run_id))
        row = con.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
    return public_run(row)


def register_run_worker(run_id, worker_id, worker_pid=None):
    """Persist a non-sensitive worker lease before simulator automation starts."""
    worker_pid = os.getpid() if worker_pid is None else int(worker_pid)
    stamp = now()
    with connection() as con:
        con.execute('BEGIN IMMEDIATE')
        run = con.execute('SELECT status FROM runs WHERE id=?', (run_id,)).fetchone()
        if run is None:
            raise Missing('Run not found.')
        if run['status'] != 'running':
            raise Conflict('A worker can only attach to an active run.')
        current = con.execute('SELECT * FROM run_workers WHERE run_id=?', (run_id,)).fetchone()
        if current and current['active'] and current['worker_id'] != worker_id:
            raise Conflict('A worker is already registered for this run.')
        con.execute(
            '''INSERT INTO run_workers(run_id,worker_id,worker_pid,heartbeat_at,active)
               VALUES (?,?,?,?,1)
               ON CONFLICT(run_id) DO UPDATE SET worker_id=excluded.worker_id,
                 worker_pid=excluded.worker_pid,heartbeat_at=excluded.heartbeat_at,active=1''',
            (run_id, worker_id, worker_pid, stamp),
        )
    return get_run_worker(run_id)


def heartbeat_run_worker(run_id, worker_id):
    with connection() as con:
        cursor = con.execute(
            'UPDATE run_workers SET heartbeat_at=? WHERE run_id=? AND worker_id=? AND active=1',
            (now(), run_id, worker_id),
        )
        if cursor.rowcount != 1:
            raise Conflict('Worker lease is no longer active.')


def unregister_run_worker(run_id, worker_id):
    with connection() as con:
        con.execute(
            'UPDATE run_workers SET active=0,heartbeat_at=? WHERE run_id=? AND worker_id=?',
            (now(), run_id, worker_id),
        )
    return get_run_worker(run_id)


def get_run_worker(run_id):
    with connection() as con:
        row = con.execute('SELECT * FROM run_workers WHERE run_id=?', (run_id,)).fetchone()
    return dict(row) if row else None


def _pid_is_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def _recovery_state_matches(row, outcome):
    return (
        outcome == 'submitted'
        and row['status'] == 'installation_completed'
        and row['submission_state'] == 'confirmed'
        and bool(row['final_order_submitted'])
        and row['stage'] == 'order submitted'
        and row['error_code'] == 'manual_recovery'
    ) or (
        outcome == 'not_submitted'
        and row['status'] == 'failed'
        and row['submission_state'] == 'not_submitted'
        and not bool(row['final_order_submitted'])
        and row['stage'] == 'manual recovery'
        and row['error_code'] == 'manual_recovery'
    ) or (
        outcome == 'unknown'
        and row['status'] == 'running'
        and row['submission_state'] == 'ambiguous'
        and not bool(row['final_order_submitted'])
        and row['stage'] == 'submission outcome ambiguous'
        and row['error_code'] == 'manual_recovery'
    )


def recover_stale_run(
    run_id, *, outcome, worker_stopped, local_worker_active=False,
    stale_after_seconds=STALE_RUN_AFTER_SECONDS,
):
    """Idempotently recover only a stale run after explicit operator verification."""
    if outcome not in {'submitted', 'not_submitted', 'unknown'}:
        raise ValueError('Choose submitted, not_submitted, or unknown.')
    if type(worker_stopped) is not bool or type(local_worker_active) is not bool:
        raise ValueError('Worker confirmation values must be boolean.')
    if stale_after_seconds < 0:
        raise ValueError('Stale interval must be nonnegative.')

    with connection() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if row is None:
            raise Missing('Run not found.')
        if _recovery_state_matches(row, outcome):
            return public_run(row)
        if row['status'] != 'running':
            raise Conflict('Run was already resolved with a different outcome.')
        try:
            updated_at = datetime.fromisoformat(row['updated_at'])
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            raise Conflict('Run timestamp is invalid; manual database review is required.') from None
        if datetime.now(timezone.utc) - updated_at < timedelta(seconds=stale_after_seconds):
            raise Conflict('Run has not exceeded the stale interval.')
        if not worker_stopped:
            raise Conflict('Confirm the API worker and simulator browser are stopped first.')
        if local_worker_active:
            raise Conflict('The run still has an active worker in this API process.')

        worker = con.execute('SELECT * FROM run_workers WHERE run_id=?', (run_id,)).fetchone()
        if worker and worker['active']:
            try:
                heartbeat = datetime.fromisoformat(worker['heartbeat_at'])
                if heartbeat.tzinfo is None:
                    heartbeat = heartbeat.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                raise Conflict('Worker lease timestamp is invalid; manual review is required.') from None
            fresh_heartbeat = (
                datetime.now(timezone.utc) - heartbeat
                < timedelta(seconds=WORKER_HEARTBEAT_TIMEOUT_SECONDS)
            )
            same_process = int(worker['worker_pid']) == os.getpid()
            if fresh_heartbeat or (not same_process and _pid_is_alive(worker['worker_pid'])):
                raise Conflict('A registered automation worker is still active.')

        state_by_outcome = {
            'submitted': ('confirmed', 'installation_completed', 'order submitted', 1),
            'not_submitted': ('not_submitted', 'failed', 'manual recovery', 0),
            'unknown': ('ambiguous', 'running', 'submission outcome ambiguous', 0),
        }
        submission_state, status, stage, submitted_flag = state_by_outcome[outcome]
        if row['submission_state'] == 'confirmed' and outcome != 'submitted':
            raise Conflict('Persisted simulator confirmation cannot be reversed.')
        if row['submission_state'] == 'not_submitted' and outcome != 'not_submitted':
            raise Conflict('Persisted non-submission confirmation cannot be reversed.')
        stamp = now()
        con.execute(
            '''UPDATE runs SET status=?,stage=?,error_code='manual_recovery',
               submission_state=?,final_order_submitted=?,updated_at=?,finished_at=?
               WHERE id=?''',
            (status, stage, submission_state, submitted_flag, stamp,
             stamp if status in TERMINAL else None, run_id),
        )
        con.execute(
            'UPDATE run_workers SET active=0,heartbeat_at=? WHERE run_id=?',
            (stamp, run_id),
        )
        updated = con.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
    return public_run(updated)

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
    """Refuse to serve a database containing sensitive fields or ciphertext."""
    with connection() as con:
        for row in con.execute('SELECT id,payload FROM orders'):
            assert_non_sensitive_order(json.loads(row['payload']), row['id'])
        for row in con.execute('SELECT order_id,order_snapshot,stage FROM runs'):
            assert_non_sensitive_order(json.loads(row['order_snapshot']), row['order_id'])
            if row['stage'] not in SAFE_STAGES:
                raise ValueError('A legacy run needs migration to safe stage metadata.')
