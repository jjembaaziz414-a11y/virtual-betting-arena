from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for
import os
import random, uuid, time, threading, secrets, sqlite3, json
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from game_connector import CentralGame
    central = CentralGame("Virtual Betting Arena")
    central.start()
    CENTRAL_ENABLED = True
except Exception:
    central = None
    CENTRAL_ENABLED = False

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Render's persistent disk is mounted at /var/data. Keep SQLite and the Flask
# signing key there so deploys/restarts do not create a new season or lose sessions.
DATA_DIR = os.environ.get("DATA_DIR") or (
    "/var/data" if os.path.isdir("/var/data") and os.access("/var/data", os.W_OK) else BASE_DIR
)
os.makedirs(DATA_DIR, exist_ok=True)
SECRET_KEY_FILE = os.path.join(DATA_DIR, "session_secret.key")

def load_or_create_secret_key():
    """Keep Flask session signatures stable across normal server restarts."""
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    try:
        with open(SECRET_KEY_FILE, "r", encoding="utf-8") as key_file:
            saved_key = key_file.read().strip()
        if saved_key:
            return saved_key
    except FileNotFoundError:
        pass
    new_key = secrets.token_hex(32)
    try:
        fd = os.open(SECRET_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as key_file:
            key_file.write(new_key)
        return new_key
    except FileExistsError:
        with open(SECRET_KEY_FILE, "r", encoding="utf-8") as key_file:
            return key_file.read().strip()

app.secret_key = load_or_create_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('HTTPS_ENABLED', '0') == '1',
    SESSION_PERMANENT=True,
    PERMANENT_SESSION_LIFETIME=31536000  # keep device identity for one year
)

lock = threading.Lock()
DB_FILE = os.path.join(DATA_DIR, "game_state.db")

# Master PIN
ADMIN_PIN = "4422"

PROTECTED_RESERVE = 30000.00
HOUSE_VAULT_INITIAL = 50000.00
BOOKMAKER_OVERROUND = 1.20
HOUSE_COMMISSION_RATE = 0.20

# Memory runtime state (Loaded from DB on start)
connected_devices = {}
device_order = []
master_admin_device_id = None

house_vault = HOUSE_VAULT_INITIAL
game_profit = 0.00
cycle_return_pool = 0.00
total_player_deposits = 0.00
cycle_mode = "balanced_payout"

round_bets_ledger = {}
settled_rounds = set()
pending_payouts = {}
round_result_cache = {}

DEVICE_COLORS = [
    "#28a745", "#ffc107", "#17a2b8", "#e83e8c",
    "#fd7e14", "#6f42c1", "#20c997", "#6c757d",
    "#00bcd4", "#ff5722", "#8bc34a", "#9c27b0"
]

BETTING_DURATION = 50.0
MATCH_DURATION = 93.0  # 45 sec first half + 3 sec break + 45 sec second half
HALF_TIME_BREAK_DURATION = 3.0
CYCLE_DURATION = BETTING_DURATION + MATCH_DURATION
GLOBAL_START_TIME = time.time()

TEAMS_POOL = [
    ("ARS", "SUN"), ("MCI", "MUN"), ("CHE", "LIV"), ("TOT", "NEW"),
    ("RMA", "BAR"), ("ATM", "VIL"), ("INT", "ACM"), ("JUV", "NAP"),
    ("BAY", "BVB"), ("RBL", "LEV"), ("PSG", "OM "), ("LYO", "ASM"),
    ("POR", "SLB"), ("AJX", "PSV"), ("GAL", "FEN"), ("CEL", "RAN")
]

# ----------------------------------------------------
# DATABASE RECOVERY ENGINE
# ----------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes persistent tables and restores memory state on server boot."""
    global house_vault, game_profit, cycle_return_pool, total_player_deposits, GLOBAL_START_TIME
    global master_admin_device_id

    with get_db() as conn:
        cursor = conn.cursor()
        
        # System status ledger
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_state (
                key TEXT PRIMARY KEY,
                val_num REAL,
                val_str TEXT
            )
        ''')
        
        # Device accounts
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS devices (
                dev_id TEXT PRIMARY KEY,
                number INTEGER,
                color TEXT,
                balance REAL
            )
        ''')
        
        # Round bets ledger
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bets (
                id TEXT PRIMARY KEY,
                dev_id TEXT,
                round_idx INTEGER,
                stake REAL,
                effective_stake REAL,
                market TEXT,
                selection_type TEXT,
                odds REAL
            )
        ''')
        
        # Pending payouts
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_payouts (
                id TEXT PRIMARY KEY,
                dev_id TEXT,
                amount REAL,
                ref TEXT,
                round_idx INTEGER,
                reason TEXT,
                funding_source TEXT,
                attempts INTEGER,
                status TEXT,
                local_credited INTEGER,
                created_at REAL,
                last_attempt REAL,
                last_error TEXT
            )
        ''')
        
        # Settled rounds tracker
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settled_rounds (
                round_idx INTEGER PRIMARY KEY
            )
        ''')

        # Persist locked match outcomes so a server restart cannot reroll a live match.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS match_results (
                round_idx INTEGER PRIMARY KEY,
                ft_outcome TEXT NOT NULL,
                ht_outcome TEXT NOT NULL,
                final_home_goals INTEGER NOT NULL,
                final_away_goals INTEGER NOT NULL,
                final_home_ht_goals INTEGER NOT NULL,
                final_away_ht_goals INTEGER NOT NULL,
                return_pool_selected INTEGER NOT NULL
            )
        ''')

        # Persistent history of winning bets for the admin's recent-results panel
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS winning_history (
                bet_id TEXT PRIMARY KEY,
                round_idx INTEGER NOT NULL,
                dev_id TEXT,
                market TEXT,
                selection_type TEXT,
                odds REAL NOT NULL,
                payout REAL NOT NULL,
                created_at REAL NOT NULL
            )
        ''')
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS player_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                wallet_dev_id TEXT NOT NULL UNIQUE,
                created_at REAL NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS deposit_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_dev_id TEXT NOT NULL,
                amount REAL NOT NULL,
                method TEXT NOT NULL,
                payment_reference TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL,
                reviewed_at REAL,
                reviewed_by TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS wallet_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_dev_id TEXT NOT NULL,
                amount REAL NOT NULL,
                kind TEXT NOT NULL,
                reference TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.commit()

    # Keep the match-cycle timeline stable through a normal process restart.
    with get_db() as conn:
        row = conn.execute("SELECT val_num FROM system_state WHERE key='global_start_time'").fetchone()
        if row and row[0] is not None:
            GLOBAL_START_TIME = float(row[0])
        else:
            GLOBAL_START_TIME = time.time()
            conn.execute(
                "INSERT OR REPLACE INTO system_state (key, val_num, val_str) VALUES (?, ?, ?)",
                ("global_start_time", GLOBAL_START_TIME, None)
            )
            conn.commit()

    # RESTORE STATE FROM DB
    restore_system_state()

def save_system_metric(key, val_num, val_str=None):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO system_state (key, val_num, val_str) VALUES (?, ?, ?)",
            (key, val_num, val_str)
        )
        conn.commit()

def restore_system_state():
    global house_vault, game_profit, cycle_return_pool, total_player_deposits
    global master_admin_device_id, connected_devices, device_order, pending_payouts, settled_rounds, round_result_cache

    with get_db() as conn:
        cursor = conn.cursor()
        
        # Load System Metrics
        metrics = {row['key']: (row['val_num'], row['val_str']) for row in cursor.execute("SELECT * FROM system_state")}
        
        house_vault = metrics.get('house_vault', (HOUSE_VAULT_INITIAL, None))[0]
        game_profit = metrics.get('game_profit', (0.0, None))[0]
        cycle_return_pool = metrics.get('cycle_return_pool', (0.0, None))[0]
        total_player_deposits = metrics.get('total_player_deposits', (0.0, None))[0]
        master_admin_device_id = metrics.get('master_admin_device_id', (0.0, None))[1]

        # Restore Connected Devices
        for row in cursor.execute("SELECT * FROM devices ORDER BY number ASC"):
            connected_devices[row['dev_id']] = {
                "number": row['number'],
                "color": row['color'],
                "balance": float(row['balance'])
            }
            if row['dev_id'] not in device_order:
                device_order.append(row['dev_id'])

        # Restore Round Bets
        for row in cursor.execute("SELECT * FROM bets"):
            r_idx = row['round_idx']
            round_bets_ledger.setdefault(r_idx, []).append({
                "id": row['id'],
                "dev_id": row['dev_id'],
                "stake": row['stake'],
                "effective_stake": row['effective_stake'],
                "market": row['market'],
                "selection_type": row['selection_type'],
                "odds": row['odds']
            })

        # Restore Pending Payouts
        for row in cursor.execute("SELECT * FROM pending_payouts"):
            pending_payouts[row['id']] = {
                "id": row['id'],
                "dev_id": row['dev_id'],
                "amount": float(row['amount']),
                "ref": row['ref'],
                "round_idx": row['round_idx'],
                "reason": row['reason'],
                "funding_source": row['funding_source'],
                "attempts": row['attempts'],
                "status": row['status'],
                "local_credited": bool(row['local_credited']),
                "created_at": row['created_at'],
                "last_attempt": row['last_attempt'],
                "last_error": row['last_error']
            }

        # Restore Settled Rounds
        for row in cursor.execute("SELECT round_idx FROM settled_rounds"):
            settled_rounds.add(row['round_idx'])

        # Restore locked scores/outcomes, preventing a restart from changing a match.
        for row in cursor.execute("SELECT * FROM match_results"):
            round_result_cache[int(row['round_idx'])] = {
                "ft_outcome": row['ft_outcome'],
                "ht_outcome": row['ht_outcome'],
                "final_home_goals": int(row['final_home_goals']),
                "final_away_goals": int(row['final_away_goals']),
                "final_home_ht_goals": int(row['final_home_ht_goals']),
                "final_away_ht_goals": int(row['final_away_ht_goals']),
                "return_pool_selected": bool(row['return_pool_selected'])
            }

def sync_device_to_db(dev_id):
    if dev_id in connected_devices:
        d = connected_devices[dev_id]
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO devices (dev_id, number, color, balance) VALUES (?, ?, ?, ?)",
                (dev_id, d["number"], d["color"], d["balance"])
            )
            conn.commit()

def sync_pending_payout_to_db(payout_id):
    if payout_id in pending_payouts:
        p = pending_payouts[payout_id]
        with get_db() as conn:
            conn.execute('''
                INSERT OR REPLACE INTO pending_payouts 
                (id, dev_id, amount, ref, round_idx, reason, funding_source, attempts, status, local_credited, created_at, last_attempt, last_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                p['id'], p['dev_id'], p['amount'], p['ref'], p['round_idx'],
                p['reason'], p['funding_source'], p['attempts'], p['status'],
                1 if p['local_credited'] else 0, p['created_at'], p['last_attempt'], p['last_error']
            ))
            conn.commit()

def log_system_audit(action_tag=""):
    total_players = sum(d["balance"] for d in connected_devices.values())
    pending_sum = sum(p["amount"] for p in pending_payouts.values() if p.get("status") == "pending" and not p.get("local_credited", False))
    grand_total = round(total_players + game_profit + cycle_return_pool + pending_sum, 2)
    print(f"[SYSTEM AUDIT | {action_tag}] Players: UGX {total_players:,.2f} | Profit: UGX {game_profit:,.2f} | Return Pool: UGX {cycle_return_pool:,.2f} | Pending: UGX {pending_sum:,.2f} | GRAND TOTAL: UGX {grand_total:,.2f}")
    return grand_total

def central_income(amount, ref):
    if central:
        try: central.income(amount, ref)
        except Exception: pass

def central_expense(amount, ref):
    if central:
        try: central.expense(amount, ref)
        except Exception: pass

def central_payout(recipient, amount, ref):
    if not central:
        return True
    try:
        result = central.payout(recipient, amount, ref)
        return result is not False
    except Exception:
        return False

def queue_pending_payout(dev_id, amount, ref, round_idx, reason, funding_source="return_pool"):
    payout_id = f"{round_idx}:{dev_id}:{uuid.uuid4().hex[:10]}"
    pending_payouts[payout_id] = {
        "id": payout_id,
        "dev_id": dev_id,
        "amount": round(float(amount), 2),
        "ref": ref,
        "round_idx": round_idx,
        "reason": reason,
        "funding_source": funding_source,
        "attempts": 0,
        "status": "pending",
        "local_credited": False,
        "created_at": time.time(),
        "last_attempt": None,
        "last_error": reason
    }
    sync_pending_payout_to_db(payout_id)
    log_system_audit("PAYOUT QUEUED")
    return payout_id

def retry_pending_payout(payout_id):
    global cycle_return_pool
    record = pending_payouts.get(payout_id)
    if not record:
        return False, "Pending payout not found."
    if record["status"] == "paid":
        return True, "Payout already completed."

    record["attempts"] += 1
    record["last_attempt"] = time.time()
    if not record.get("local_credited", False):
        dev_id = record["dev_id"]
        if dev_id not in connected_devices:
            record["last_error"] = "Winning device is no longer connected."
            sync_pending_payout_to_db(payout_id)
            return False, record["last_error"]
        payout_amount = round(float(record["amount"]), 2)

        if payout_amount > round(cycle_return_pool, 2):
            record["last_error"] = (
                f"Insufficient return-pool funds for retry: UGX {cycle_return_pool:,.2f} available, "
                f"UGX {payout_amount:,.2f} required."
            )
            sync_pending_payout_to_db(payout_id)
            return False, record["last_error"]

        cycle_return_pool = round(cycle_return_pool - payout_amount, 2)
        save_system_metric('cycle_return_pool', cycle_return_pool)
        
        connected_devices[dev_id]["balance"] = round(connected_devices[dev_id]["balance"] + payout_amount, 2)
        sync_device_to_db(dev_id)
        
        record["local_credited"] = True

    ok = central_payout(record["dev_id"], record["amount"], record["ref"])
    if ok:
        record["status"] = "paid"
        record["last_error"] = ""
        sync_pending_payout_to_db(payout_id)
        log_system_audit("PAYOUT RETRY SUCCESS")
        return True, "Payout retry successful."
        
    record["status"] = "pending"
    record["last_error"] = "Central payout delivery failed."
    sync_pending_payout_to_db(payout_id)
    return False, "Payout is still pending."

def retry_fundable_pending_payouts(current_round_idx):
    candidates = [
        p for p in pending_payouts.values()
        if p.get("status") == "pending"
        and int(p.get("round_idx", current_round_idx)) < int(current_round_idx)
    ]
    for record in sorted(candidates, key=lambda p: (int(p.get("round_idx", 0)), p.get("created_at", 0))):
        ok, _ = retry_pending_payout(record["id"])
        if not ok:
            continue

def ensure_device(dev_id=None):
    with lock:
        if dev_id is None:
            dev_id = str(uuid.uuid4())
        if dev_id not in connected_devices:
            color_idx = len(connected_devices) % len(DEVICE_COLORS)
            dev_num = len(connected_devices) + 1
            connected_devices[dev_id] = {
                "number": dev_num,
                "color": DEVICE_COLORS[color_idx],
                "balance": 10000.00
            }
            device_order.append(dev_id)
            sync_device_to_db(dev_id)
    return dev_id

@app.before_request
def track_device():
    # A logged-in player maps every browser/session to the same stable wallet ID.
    session.permanent = True
    path = request.path
    if path in ('/login', '/register', '/logout', '/health'):
        return None
    account_id = session.get('account_id')
    if account_id:
        try:
            with get_db() as conn:
                row = conn.execute('SELECT username, wallet_dev_id FROM player_accounts WHERE id=?', (account_id,)).fetchone()
            if row:
                session['username'] = row['username']
                session['device_id'] = row['wallet_dev_id']
                ensure_device(row['wallet_dev_id'])
            else:
                session.clear()
        except Exception:
            session.clear()
    protected = (
        '/arena', '/get_state', '/place_bet', '/aviator', '/rugby',
        '/velocity', '/chicken-clash', '/hot-7-fruit', '/fortune-slots',
        '/extra-games/', '/admin', '/admin-auth', '/admin/manage', '/admin/house',
        '/admin/deposits', '/aviator/admin', '/deposit/request', '/round-game/'
    )
    if not session.get('account_id') and any(path == item or path.startswith(item) for item in protected):
        if request.method == 'GET' and not request.is_json:
            return redirect(url_for('login_page', next=path))
        return jsonify({'success': False, 'message': 'Please log in to use your shared player account.'}), 401
    if not session.get('account_id'):
        session.pop('device_id', None)

def generate_round_odds(round_idx):
    rng = random.Random(1337 + round_idx)
    home = round(rng.uniform(2.00, 2.35), 2)
    draw = round(rng.uniform(2.60, 3.50), 2)
    if draw <= home:
        draw = round(home + 0.50, 2)

    big_odd = rng.choice([4.00, 6.00, 7.00, 10.00])
    big_side = rng.choice(["1", "X", "2"])

    if big_side == "1": home = big_odd
    elif big_side == "X": draw = big_odd

    if big_side == "2": away = big_odd
    else:
        away = round(rng.uniform(max(3.20, draw + 0.25), 4.50), 2)
        if away <= draw: away = 4.50

    return {
        "1": round(max(2.00, min(home, 10.00)), 2),
        "X": round(max(2.00, min(draw, 10.00)), 2),
        "2": round(max(2.00, min(away, 10.00)), 2)
    }

def generate_half_time_odds(round_idx):
    rng = random.Random(7331 + round_idx)
    return {
        "1": round(rng.uniform(1.80, 2.60), 2),
        "X": round(rng.uniform(2.40, 3.80), 2),
        "2": round(rng.uniform(2.00, 4.50), 2)
    }

def generate_exact_match_score(ft_outcome, ht_outcome, rng):
    if ft_outcome == "1":
        home_ft = rng.randint(1, 4)
        away_ft = rng.randint(0, home_ft - 1)
    elif ft_outcome == "2":
        away_ft = rng.randint(1, 4)
        home_ft = rng.randint(0, away_ft - 1)
    else:
        home_ft = rng.randint(0, 3)
        away_ft = home_ft

    if ht_outcome == "1":
        home_ht = rng.randint(1, max(1, home_ft))
        away_ht = rng.randint(0, min(away_ft, max(0, home_ht - 1)))
    elif ht_outcome == "2":
        away_ht = rng.randint(1, max(1, away_ft))
        home_ht = rng.randint(0, min(home_ft, max(0, away_ht - 1)))
    else:
        shared_ht = min(home_ft, away_ft)
        home_ht = rng.randint(0, shared_ht)
        away_ht = home_ht

    home_ht = min(home_ht, home_ft)
    away_ht = min(away_ht, away_ft)

    return home_ft, away_ft, home_ht, away_ht

def get_current_round_data(auto_settle=True):
    now = time.time()
    elapsed_total = max(0, now - GLOBAL_START_TIME)
    round_idx = int(elapsed_total // CYCLE_DURATION)
    phase_elapsed = elapsed_total % CYCLE_DURATION

    if auto_settle:
        retry_fundable_pending_payouts(round_idx)

    if phase_elapsed < BETTING_DURATION:
        phase = "betting"
        time_left = BETTING_DURATION - phase_elapsed
        match_elapsed = 0.0
        match_finished = False
    else:
        phase = "live"
        match_elapsed = phase_elapsed - BETTING_DURATION
        time_left = CYCLE_DURATION - phase_elapsed
        match_finished = match_elapsed >= MATCH_DURATION

    match_rng = random.Random(9999 + round_idx)
    teams = match_rng.choice(TEAMS_POOL)
    odds = generate_round_odds(round_idx)
    ht_odds = generate_half_time_odds(round_idx)

    global cycle_mode, cycle_return_pool

    rng = random.Random(1337 + round_idx)
    bets_for_round = list(round_bets_ledger.get(round_idx, []))

    projected_payouts = {"1": 0.0, "X": 0.0, "2": 0.0}
    ht_projected_payouts = {"1": 0.0, "X": 0.0, "2": 0.0}

    for bet in bets_for_round:
        key = bet.get("selection_type")
        bs = round(float(bet.get("effective_stake", bet.get("stake", 0))), 2)
        bo = round(float(bet.get("odds", 0)), 2)
        if bet.get("market", "ft_result") == "ht_result":
            if key in ht_projected_payouts:
                ht_projected_payouts[key] = round(ht_projected_payouts[key] + bs * bo, 2)
        else:
            if key in projected_payouts:
                projected_payouts[key] = round(projected_payouts[key] + bs * bo, 2)

    return_pool_selected = False

    # Select FT + HT as ONE liability. Both markets can win in the same
    # match, so the combined payout must fit inside the available pool.
    if bets_for_round:
        ft_keys = list(projected_payouts.keys())
        ht_keys = list(ht_projected_payouts.keys())
        combinations = []

        for ft_key in ft_keys:
            for ht_key in ht_keys:
                combined_payout = round(
                    projected_payouts[ft_key] + ht_projected_payouts[ht_key], 2
                )
                combinations.append((ft_key, ht_key, combined_payout))

        affordable_combinations = [
            combo for combo in combinations if combo[2] <= cycle_return_pool
        ]

        if affordable_combinations:
            weights = [
                1.0 / max(0.01, odds[ft_key] * ht_odds[ht_key])
                for ft_key, ht_key, _ in affordable_combinations
            ]
            selected = rng.choices(affordable_combinations, weights=weights, k=1)[0]
            ft_outcome, ht_outcome, selected_payout = selected
            if selected_payout > 0:
                return_pool_selected = True
        else:
            lowest_payout = min(combo[2] for combo in combinations)
            lowest_candidates = [
                combo for combo in combinations if combo[2] == lowest_payout
            ]
            ft_outcome, ht_outcome, _ = rng.choice(lowest_candidates)
    else:
        prob_home_win = 1.0 / odds["1"]
        prob_draw = 1.0 / odds["X"]
        prob_away_win = 1.0 / odds["2"]
        total_prob = prob_home_win + prob_draw + prob_away_win
        roll = rng.random()
        if roll < prob_home_win / total_prob: ft_outcome = "1"
        elif roll < (prob_home_win + prob_draw) / total_prob: ft_outcome = "X"
        else: ft_outcome = "2"
        ht_outcome = "X"

    if phase == "live" and round_idx in round_result_cache:
        locked = round_result_cache[round_idx]
        ft_outcome = locked["ft_outcome"]
        ht_outcome = locked["ht_outcome"]
        home_ft = locked["final_home_goals"]
        away_ft = locked["final_away_goals"]
        home_ht = locked["final_home_ht_goals"]
        away_ht = locked["final_away_ht_goals"]
        return_pool_selected = locked["return_pool_selected"]
    else:
        home_ft, away_ft, home_ht, away_ht = generate_exact_match_score(ft_outcome, ht_outcome, rng)
        if phase == "live":
            locked_result = {
                "ft_outcome": ft_outcome,
                "ht_outcome": ht_outcome,
                "final_home_goals": home_ft,
                "final_away_goals": away_ft,
                "final_home_ht_goals": home_ht,
                "final_away_ht_goals": away_ht,
                "return_pool_selected": return_pool_selected
            }
            round_result_cache[round_idx] = locked_result
            with get_db() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO match_results
                    (round_idx, ft_outcome, ht_outcome, final_home_goals, final_away_goals,
                     final_home_ht_goals, final_away_ht_goals, return_pool_selected)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (round_idx, ft_outcome, ht_outcome, home_ft, away_ft,
                      home_ht, away_ht, 1 if return_pool_selected else 0))
                conn.commit()

    match_events = []
    ht_goal_pool = (['home'] * home_ht) + (['away'] * away_ht)
    rng.shuffle(ht_goal_pool)
    if ht_goal_pool:
        ht_minutes = sorted(rng.sample(range(5, 44), len(ht_goal_pool)))
        for side, minute in zip(ht_goal_pool, ht_minutes):
            match_events.append({"minute": minute, "type": "goal", "side": side})

    sh_home = home_ft - home_ht
    sh_away = away_ft - away_ht
    sh_goal_pool = (['home'] * sh_home) + (['away'] * sh_away)
    rng.shuffle(sh_goal_pool)
    if sh_goal_pool:
        sh_minutes = sorted(rng.sample(range(48, 88), len(sh_goal_pool)))
        for side, minute in zip(sh_goal_pool, sh_minutes):
            match_events.append({"minute": minute, "type": "goal", "side": side})

    event_types = ['dangerous_attack', 'ball_saved', 'corner_kick', 'free_kick']
    for _ in range(rng.randint(6, 10)):
        minute = rng.randint(2, 88)
        match_events.append({"minute": minute, "type": rng.choice(event_types), "side": rng.choice(['home', 'away'])})

    match_events.sort(key=lambda x: x['minute'])

    round_data = {
        "round_idx": round_idx,
        "phase": phase,
        "time_left": round(time_left, 1),
        "match_elapsed": round(match_elapsed, 2),
        "match_finished": bool(match_finished),
        "half_time_break": bool(
            phase == "live"
            and 45.0 <= match_elapsed < 45.0 + HALF_TIME_BREAK_DURATION
        ),
        "home": teams[0],
        "away": teams[1],
        "odds": odds,
        "ht_odds": ht_odds,
        "match_events": match_events,
        "target_ft_outcome": ft_outcome,
        "target_ht_outcome": ht_outcome,
        "final_home_goals": home_ft,
        "final_away_goals": away_ft,
        "demo_cycle_mode": cycle_mode,
        "demo_return_pool": cycle_return_pool,
        "return_pool_selected": return_pool_selected
    }

    if auto_settle:
        settle_round_if_needed(round_data)

    return round_data

def prune_old_states(current_round_idx):
    global round_bets_ledger, settled_rounds, round_result_cache
    if len(round_bets_ledger) > 20:
        old_rounds = [r for r in round_bets_ledger.keys() if r < current_round_idx - 10]
        for r in old_rounds:
            round_bets_ledger.pop(r, None)
            settled_rounds.discard(r)
    for r in list(round_result_cache):
        if r < current_round_idx - 10:
            round_result_cache.pop(r, None)

def settle_round_if_needed(round_data):
    global house_vault, cycle_return_pool
    round_idx = round_data['round_idx']
    if round_data['phase'] != 'live':
        return

    with lock:
        if round_idx in settled_rounds:
            return

        prune_old_states(round_idx)
        bets = list(round_bets_ledger.get(round_idx, []))
        if not bets:
            settled_rounds.add(round_idx)
            with get_db() as conn:
                conn.execute("INSERT OR IGNORE INTO settled_rounds (round_idx) VALUES (?)", (round_idx,))
                conn.commit()
            return

        final_home_goals = int(round_data.get('final_home_goals', 0))
        final_away_goals = int(round_data.get('final_away_goals', 0))
        if final_home_goals > final_away_goals: ft_outcome = "1"
        elif final_home_goals < final_away_goals: ft_outcome = "2"
        else: ft_outcome = "X"

        first_half_home = 0
        first_half_away = 0
        for event in round_data.get('match_events', []):
            if event.get('type') != 'goal': continue
            if int(event.get('minute', 0)) <= 45:
                if event.get('side') == 'home': first_half_home += 1
                elif event.get('side') == 'away': first_half_away += 1

        if first_half_home > first_half_away: ht_outcome = "1"
        elif first_half_home < first_half_away: ht_outcome = "2"
        else: ht_outcome = "X"

        winning_bets = []
        total_winning_payout = 0.0

        def bet_wins(bet):
            selection = bet.get('selection_type')
            market = bet.get('market', 'ft_result')
            return selection == (ht_outcome if market == 'ht_result' else ft_outcome)

        for bet in bets:
            dev_id = bet['dev_id']
            if dev_id not in connected_devices: continue
            if bet_wins(bet):
                payout = round(float(bet['effective_stake']) * float(bet['odds']), 2)
                winning_bets.append((bet, payout))
                total_winning_payout = round(total_winning_payout + payout, 2)

        # Record winning selections once, including wins queued for later payment.
        with get_db() as conn:
            for bet, payout in winning_bets:
                bet_id = str(bet.get("id") or f"{round_idx}:{bet['dev_id']}:{bet.get('market')}:{bet.get('selection_type')}")
                conn.execute(
                    "INSERT OR IGNORE INTO winning_history (bet_id, round_idx, dev_id, market, selection_type, odds, payout, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (bet_id, round_idx, bet.get("dev_id"), bet.get("market", "ft_result"), bet.get("selection_type"), float(bet.get("odds", 0)), float(payout), time.time())
                )
            conn.commit()

        if total_winning_payout > round(cycle_return_pool, 2):
            for bet, payout in winning_bets:
                dev_id = bet["dev_id"]
                ref = f"win:round:{round_idx}:bet:{bet.get('id', uuid.uuid4().hex[:8])}"
                queue_pending_payout(
                    dev_id, payout, ref, round_idx,
                    "Winning payout queued: waiting for return pool funds.",
                    funding_source="return_pool"
                )
            settled_rounds.add(round_idx)
            with get_db() as conn:
                conn.execute("INSERT OR IGNORE INTO settled_rounds (round_idx) VALUES (?)", (round_idx,))
                conn.commit()
            log_system_audit(f"ROUND {round_idx} SETTLED (PAYOUTS QUEUED)")
            return

        for bet, payout in winning_bets:
            dev_id = bet['dev_id']
            connected_devices[dev_id]["balance"] = round(connected_devices[dev_id]["balance"] + payout, 2)
            sync_device_to_db(dev_id)

            cycle_return_pool = round(cycle_return_pool - payout, 2)
            save_system_metric('cycle_return_pool', cycle_return_pool)

            ref = f"win:round:{round_idx}:bet:{bet.get('id', uuid.uuid4().hex[:8])}"
            if not central_payout(dev_id, payout, ref):
                payout_id = queue_pending_payout(
                    dev_id, payout, ref, round_idx,
                    "Central payout delivery failed.",
                    funding_source="return_pool"
                )
                pending_payouts[payout_id]["local_credited"] = True
                sync_pending_payout_to_db(payout_id)

        settled_rounds.add(round_idx)
        with get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO settled_rounds (round_idx) VALUES (?)", (round_idx,))
            conn.commit()
        log_system_audit(f"ROUND {round_idx} SETTLED")

# Initialize persistent state when imported by Gunicorn/Render.
init_db()

@app.route('/')
def portal():
    if not session.get('account_id'):
        return redirect(url_for('login_page'))
    dev_id = session.get('device_id')
    info = connected_devices.get(dev_id, {"number": 1, "color": "#22c55e", "balance": 0})
    with get_db() as conn:
        deposits = conn.execute('SELECT amount, method, payment_reference, status, created_at FROM deposit_requests WHERE wallet_dev_id=? ORDER BY id DESC LIMIT 5', (dev_id,)).fetchall()
    return render_template_string(PORTAL_TEMPLATE, device_number=info["number"], color=info["color"],
                                  account_name=session.get('username','Player'), balance=info.get('balance',0), deposit_requests=deposits)

AUTH_TEMPLATE = r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{title}} | Virtual Betting Arena</title><style>
*{box-sizing:border-box}body{margin:0;background:#07152b;color:#f8fafc;font-family:Arial,sans-serif;min-height:100vh;display:grid;place-items:center;padding:18px}.auth{width:100%;max-width:430px;background:#102443;border:1px solid #25476f;border-radius:18px;padding:24px;box-shadow:0 20px 60px #0007}.brand{font-weight:900;color:#29d17d;letter-spacing:.5px;font-size:23px}.sub{color:#b7c7dc;font-size:13px;line-height:1.5}.field{width:100%;padding:13px;border-radius:9px;border:1px solid #345579;background:#08172d;color:#fff;margin:7px 0 13px;font-size:16px}.submit{width:100%;border:0;background:#25c76f;color:#062014;font-weight:900;padding:14px;border-radius:9px;cursor:pointer;font-size:16px}.link{color:#7de8ae}.error{background:#4b1d2b;color:#ffc4cf;padding:10px;border-radius:8px;margin:12px 0}.note{font-size:12px;color:#9eb1ca;margin-top:15px}</style></head><body><main class="auth"><div class="brand">⚡ VIRTUAL BETTING ARENA</div><h2>{{title}}</h2><p class="sub">One player account and one shared wallet for Football, Aviator, Rugby, Racing, Chicken Clash, Hot 7 Fruit and Fortune Slots.</p>{% if error %}<div class="error">{{error}}</div>{% endif %}<form method="post"><label>Username</label><input class="field" name="username" required minlength="3" maxlength="30" autocomplete="username" placeholder="Choose a username"><label>Password</label><input class="field" name="password" type="password" required minlength="6" autocomplete="{{'new-password' if mode=='register' else 'current-password'}}" placeholder="At least 6 characters">{% if mode=='register' %}<label>Confirm password</label><input class="field" name="confirm" type="password" required minlength="6" autocomplete="new-password" placeholder="Repeat password">{% endif %}<button class="submit" type="submit">{{'CREATE ACCOUNT' if mode=='register' else 'LOG IN'}}</button></form><p>{% if mode=='register' %}Already registered? <a class="link" href="/login">Log in</a>{% else %}New player? <a class="link" href="/register">Create account</a>{% endif %}</p><div class="note">Demo wallet starting credit is UGX 10,000. Deposit requests are credited only after an admin verifies the payment reference. No payment provider is connected yet.</div></main></body></html>'''

@app.route('/register', methods=['GET','POST'])
def register_page():
    if session.get('account_id'):
        return redirect(url_for('portal'))
    error = ''
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        confirm = request.form.get('confirm') or ''
        if not username or len(username) < 3 or len(username) > 30 or not username.replace('_','').isalnum():
            error = 'Username must be 3–30 letters, numbers or underscores.'
        elif len(password) < 6:
            error = 'Password must contain at least 6 characters.'
        elif password != confirm:
            error = 'Passwords do not match.'
        else:
            try:
                old_id = session.get('device_id')
                wallet_id = old_id if old_id in connected_devices else 'acct-' + uuid.uuid4().hex
                with get_db() as conn:
                    cur = conn.execute('INSERT INTO player_accounts(username,password_hash,wallet_dev_id,created_at) VALUES(?,?,?,?)',
                                       (username, generate_password_hash(password), wallet_id, time.time()))
                    account_id = cur.lastrowid
                    conn.commit()
                ensure_device(wallet_id)
                with get_db() as conn:
                    conn.execute('INSERT INTO wallet_transactions(wallet_dev_id,amount,kind,reference,created_at) VALUES(?,?,?,?,?)',
                                 (wallet_id, 10000.0, 'DEMO_START', 'Initial demo wallet credit', time.time()))
                    conn.commit()
                session.clear(); session.permanent = True
                session['account_id'] = account_id; session['username'] = username; session['device_id'] = wallet_id
                return redirect(url_for('portal'))
            except sqlite3.IntegrityError:
                error = 'That username is already taken. Please choose another.'
            except Exception:
                app.logger.exception('Account registration failed')
                error = 'Account creation failed. Please try again.'
    return render_template_string(AUTH_TEMPLATE, title='Create account', mode='register', error=error)

@app.route('/login', methods=['GET','POST'])
def login_page():
    if session.get('account_id'):
        return redirect(url_for('portal'))
    error = ''
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        with get_db() as conn:
            row = conn.execute('SELECT id, username, password_hash, wallet_dev_id FROM player_accounts WHERE username=? COLLATE NOCASE', (username,)).fetchone()
        if not row or not check_password_hash(row['password_hash'], password):
            error = 'Incorrect username or password.'
        else:
            session.clear(); session.permanent = True
            session['account_id'] = row['id']; session['username'] = row['username']; session['device_id'] = row['wallet_dev_id']
            ensure_device(row['wallet_dev_id'])
            return redirect(url_for('portal'))
    return render_template_string(AUTH_TEMPLATE, title='Player login', mode='login', error=error)

@app.route('/logout')
def logout_page():
    session.clear()
    return redirect(url_for('login_page'))

@app.route('/deposit/request', methods=['POST'])
def deposit_request():
    dev_id = session.get('device_id')
    try: amount = round(float(request.form.get('amount','0')), 2)
    except (TypeError, ValueError): amount = 0
    method = (request.form.get('method') or '').strip()[:40]
    ref = (request.form.get('reference') or '').strip()[:120]
    if amount < 1000 or amount > 10000000 or not method or not ref:
        return redirect(url_for('portal', deposit_error='Enter a valid amount (UGX 1,000–10,000,000), payment method and reference.'))
    with get_db() as conn:
        conn.execute('INSERT INTO deposit_requests(wallet_dev_id,amount,method,payment_reference,status,created_at) VALUES(?,?,?,?,?,?)',
                     (dev_id, amount, method, ref, 'pending', time.time()))
        conn.commit()
    return redirect(url_for('portal', deposit_message='Deposit request submitted. It will be credited after payment verification.'))

@app.route('/admin/deposits', methods=['GET','POST'])
def admin_deposits():
    global total_player_deposits
    dev_id = session.get('device_id')
    if not session.get('is_admin') or dev_id != master_admin_device_id:
        return redirect(url_for('portal', error='Open Admin Panel from the registered admin account first.'))
    if request.method == 'POST':
        try: req_id = int(request.form.get('request_id','0'))
        except ValueError: req_id = 0
        action = request.form.get('action')
        with lock, get_db() as conn:
            row = conn.execute("SELECT * FROM deposit_requests WHERE id=? AND status='pending'", (req_id,)).fetchone()
            if row:
                if action == 'approve':
                    wallet = connected_devices.get(row['wallet_dev_id'])
                    if wallet is None:
                        ensure_device(row['wallet_dev_id']); wallet = connected_devices.get(row['wallet_dev_id'])
                    wallet['balance'] = round(float(wallet['balance']) + float(row['amount']), 2)
                    sync_device_to_db(row['wallet_dev_id'])
                    total_player_deposits = round(float(total_player_deposits) + float(row['amount']), 2)
                    save_system_metric('total_player_deposits', total_player_deposits)
                    conn.execute("UPDATE deposit_requests SET status='approved', reviewed_at=?, reviewed_by=? WHERE id=?",
                                 (time.time(), session.get('username','admin'), req_id))
                    conn.execute('INSERT INTO wallet_transactions(wallet_dev_id,amount,kind,reference,created_at) VALUES(?,?,?,?,?)',
                                 (row['wallet_dev_id'], float(row['amount']), 'DEPOSIT', row['payment_reference'], time.time()))
                elif action == 'reject':
                    conn.execute("UPDATE deposit_requests SET status='rejected', reviewed_at=?, reviewed_by=? WHERE id=?",
                                 (time.time(), session.get('username','admin'), req_id))
                conn.commit()
        return redirect(url_for('admin_deposits'))
    with get_db() as conn:
        rows = conn.execute('SELECT d.*, a.username FROM deposit_requests d LEFT JOIN player_accounts a ON a.wallet_dev_id=d.wallet_dev_id ORDER BY d.id DESC LIMIT 100').fetchall()
    return render_template_string(r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Deposit Requests</title><style>body{background:#08162a;color:#fff;font-family:Arial;padding:16px}.wrap{max-width:900px;margin:auto}.item{background:#132947;border:1px solid #2d4a6b;border-radius:10px;padding:14px;margin:10px 0}.muted{color:#b3c4da}button{padding:10px;border:0;border-radius:6px;font-weight:bold;margin-right:5px}.approve{background:#22c55e}.reject{background:#ef4444;color:white}a{color:#86efac}</style></head><body><div class="wrap"><a href="/admin">← Admin panel</a><h1>Deposit requests</h1><p class="muted">Approve only after independently verifying the payment with your mobile-money provider. This page does not itself collect payments.</p>{% for r in rows %}<div class="item"><b>{{r.username or 'Unknown account'}}</b> — UGX {{'%.0f'|format(r.amount)}}<p class="muted">Method: {{r.method}} · Reference: {{r.payment_reference}}<br>Status: {{r.status}} · Submitted: {{r.created_at|int}}</p>{% if r.status=='pending' %}<form method="post"><input type="hidden" name="request_id" value="{{r.id}}"><button class="approve" name="action" value="approve">APPROVE & CREDIT WALLET</button><button class="reject" name="action" value="reject">REJECT</button></form>{% endif %}</div>{% else %}<p>No deposit requests yet.</p>{% endfor %}</div></body></html>''', rows=rows)

@app.route('/admin-auth', methods=['POST'])
def admin_auth():
    global master_admin_device_id
    pin = request.form.get('pin', '')
    dev_id = session.get('device_id')
    if pin == ADMIN_PIN:
        with lock:
            if master_admin_device_id is None: 
                master_admin_device_id = dev_id
                save_system_metric('master_admin_device_id', 0.0, dev_id)
            if master_admin_device_id != dev_id:
                return redirect(url_for('portal', error="Admin panel locked to another device!"))
        session['is_admin'] = True
        return redirect(url_for('admin'))
    return redirect(url_for('portal', error="Invalid PIN"))

@app.route('/arena')
def arena():
    dev_id = session.get('device_id')
    info = connected_devices.get(dev_id, {"number": 1, "color": "#28a745", "balance": 0})
    return render_template_string(MAIN_TEMPLATE, device_id=dev_id, device_number=info["number"], color=info["color"], balance=info["balance"])

@app.route('/get_state')
def get_state():
    dev_id = session.get('device_id')
    data = get_current_round_data()
    info = connected_devices.get(dev_id, {"balance": 10000.00})

    current_bets = []
    for b in round_bets_ledger.get(data["round_idx"], []):
        if b.get("dev_id") == dev_id:
            current_bets.append({
                "id": b.get("id"),
                "market": b.get("market", "ft_result"),
                "selection_type": b.get("selection_type"),
                "stake": round(float(b.get("effective_stake", b.get("stake", 0))), 2),
                "odds": round(float(b.get("odds", 0)), 2)
            })

    # The previous round is globally locked and therefore identical for every
    # connected browser. Show this player's exact win/loss outcome for it.
    last_round = data["round_idx"] - 1
    last_result = round_result_cache.get(last_round)
    last_player_bets = [
        b for b in round_bets_ledger.get(last_round, [])
        if b.get("dev_id") == dev_id
    ]
    player_results = []
    if last_result and last_player_bets:
        ft_outcome = last_result["ft_outcome"]
        ht_outcome = last_result["ht_outcome"]
        total_stake = 0.0
        total_payout = 0.0
        wins = 0
        losses = 0
        for b in last_player_bets:
            market = b.get("market", "ft_result")
            outcome = ht_outcome if market == "ht_result" else ft_outcome
            won = b.get("selection_type") == outcome
            stake = round(float(b.get("effective_stake", b.get("stake", 0))), 2)
            odds_value = round(float(b.get("odds", 0)), 2)
            payout = round(stake * odds_value, 2) if won else 0.0
            total_stake += stake
            total_payout += payout
            wins += 1 if won else 0
            losses += 0 if won else 1
            player_results.append({
                "market": market,
                "selection_type": b.get("selection_type"),
                "stake": stake,
                "odds": odds_value,
                "won": won,
                "payout": payout
            })

        last_match_rng = random.Random(9999 + last_round)
        last_teams = last_match_rng.choice(TEAMS_POOL)
        last_player_summary = {
            "round_idx": last_round,
            "home": last_teams[0],
            "away": last_teams[1],
            "final_home_goals": last_result["final_home_goals"],
            "final_away_goals": last_result["final_away_goals"],
            "ft_outcome": ft_outcome,
            "ht_outcome": ht_outcome,
            "total_stake": round(total_stake, 2),
            "total_payout": round(total_payout, 2),
            "net": round(total_payout - total_stake, 2),
            "wins": wins,
            "losses": losses,
            "bets": player_results
        }
    else:
        last_player_summary = None

    return jsonify({
        **data,
        "balance": info["balance"],
        "current_player_bets": current_bets,
        "last_player_summary": last_player_summary,
        "game_profit": game_profit,
        "protected_reserve": PROTECTED_RESERVE,
        "house_vault": house_vault,
        "pending_payouts": sum(1 for p in pending_payouts.values() if p.get("status") == "pending"),
        "strict_no_loss_mode": True,
        "house_profit_guaranteed_on_bet_rounds": True,
        "max_odds": 100.00,
        "bookmaker_overround": BOOKMAKER_OVERROUND,
        "house_commission_rate": HOUSE_COMMISSION_RATE,
        "demo_cycle_return_pool": cycle_return_pool,
        "risk_model_note": "Shared server state: all connected browsers receive the same match, odds, locked result and score.",
        "accounting_note": "Game profit cannot be negative."
    })

@app.route('/place_bet', methods=['POST'])
def place_bet():
    global game_profit, cycle_return_pool
    data = request.json or {}
    dev_id = data.get('device_id')
    try: stake = float(data.get('stake', 0))
    except: stake = 0
    selection = data.get('selection') or {}
    market = selection.get('market', 'ft_result')
    sel_type = selection.get('type')

    valid_market_types = {"ft_result": ("1", "X", "2"), "ht_result": ("1", "X", "2")}

    if dev_id != session.get('device_id'): return jsonify({"success": False, "message": "Device session mismatch."})
    if dev_id not in connected_devices: return jsonify({"success": False, "message": "Device not recognized!"})
    if stake < 100 or stake > 50000: return jsonify({"success": False, "message": "Stake out of bounds."})
    if market not in valid_market_types or sel_type not in valid_market_types[market]: return jsonify({"success": False, "message": "Invalid bet market selection!"})

    with lock:
        if connected_devices[dev_id]["balance"] < stake: return jsonify({"success": False, "message": "Insufficient balance!"})
        round_data = get_current_round_data(auto_settle=False)
        if round_data['phase'] != 'betting': return jsonify({"success": False, "message": "Betting window is closed for this round!"})

        round_idx = round_data['round_idx']
        # Multiple bets per connected device are allowed during the betting window.
        # Each submission is recorded as its own bet with its own stake/selection.

        odds_source = round_data["ht_odds"] if market == "ht_result" else round_data["odds"]
        true_odds = round(float(odds_source.get(sel_type, 0)), 2)
        if true_odds <= 0: return jsonify({"success": False, "message": "Invalid odds."})

        effective_stake = round(stake, 2)

        # Split 20% Commission / 80% Return Pool
        commission = round(effective_stake * HOUSE_COMMISSION_RATE, 2)
        pool_portion = round(effective_stake - commission, 2)

        # Safety gate: reserve existing pending liabilities and require at
        # least one complete FT+HT result to be coverable after this bet.
        projected_pool_after_bet = round(cycle_return_pool + pool_portion, 2)
        reserved_pending = round(sum(
            float(p.get("amount", 0))
            for p in pending_payouts.values()
            if p.get("status") == "pending" and not p.get("local_credited", False)
        ), 2)
        available_after_bet = round(projected_pool_after_bet - reserved_pending, 2)

        candidate_ft = {"1": 0.0, "X": 0.0, "2": 0.0}
        candidate_ht = {"1": 0.0, "X": 0.0, "2": 0.0}

        for existing_bet in round_bets_ledger.get(round_idx, []):
            existing_key = existing_bet.get("selection_type")
            existing_stake = round(float(
                existing_bet.get("effective_stake", existing_bet.get("stake", 0))
            ), 2)
            existing_odds = round(float(existing_bet.get("odds", 0)), 2)
            if existing_bet.get("market", "ft_result") == "ht_result":
                if existing_key in candidate_ht:
                    candidate_ht[existing_key] = round(
                        candidate_ht[existing_key] + existing_stake * existing_odds, 2
                    )
            elif existing_key in candidate_ft:
                candidate_ft[existing_key] = round(
                    candidate_ft[existing_key] + existing_stake * existing_odds, 2
                )

        if market == "ht_result":
            candidate_ht[sel_type] = round(
                candidate_ht[sel_type] + effective_stake * true_odds, 2
            )
        else:
            candidate_ft[sel_type] = round(
                candidate_ft[sel_type] + effective_stake * true_odds, 2
            )

        minimum_combined_liability = min(
            round(candidate_ft[ft_key] + candidate_ht[ht_key], 2)
            for ft_key in candidate_ft
            for ht_key in candidate_ht
        )

        if minimum_combined_liability > available_after_bet:
            return jsonify({
                "success": False,
                "message": (
                    f"Bet rejected for safety: no FT+HT result can be covered by "
                    f"the available Return Pool (UGX {available_after_bet:,.2f} "
                    f"available, minimum possible combined payout "
                    f"UGX {minimum_combined_liability:,.2f})."
                )
            })

        game_profit = round(game_profit + commission, 2)
        cycle_return_pool = round(cycle_return_pool + pool_portion, 2)

        save_system_metric('game_profit', game_profit)
        save_system_metric('cycle_return_pool', cycle_return_pool)

        connected_devices[dev_id]["balance"] = round(connected_devices[dev_id]["balance"] - effective_stake, 2)
        sync_device_to_db(dev_id)

        central_income(effective_stake, f"bet:device:{dev_id}:round:{round_idx}")

        bet_id = uuid.uuid4().hex[:8]
        round_bets_ledger.setdefault(round_idx, []).append({
            "id": bet_id,
            "dev_id": dev_id, "stake": stake, "effective_stake": effective_stake,
            "market": market, "selection_type": sel_type, "odds": true_odds
        })

        with get_db() as conn:
            conn.execute('''
                INSERT INTO bets (id, dev_id, round_idx, stake, effective_stake, market, selection_type, odds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (bet_id, dev_id, round_idx, stake, effective_stake, market, sel_type, true_odds))
            conn.commit()

        log_system_audit(f"BET PLACED (DEV {connected_devices[dev_id]['number']})")

    return jsonify({"success": True, "message": "Bet successfully placed!", "new_balance": connected_devices[dev_id]["balance"]})

@app.route('/admin')
def admin():
    dev_id = session.get('device_id')
    if not session.get('is_admin') or dev_id != master_admin_device_id: return redirect(url_for('portal'))
    pending = [v for v in pending_payouts.values() if v.get("status") == "pending"]
    total_players = sum(d["balance"] for d in connected_devices.values())
    pending_sum = sum(p["amount"] for p in pending if not p.get("local_credited", False))
    grand_total = round(total_players + game_profit + cycle_return_pool + pending_sum, 2)
    with get_db() as conn:
        recent_round_ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT round_idx FROM winning_history ORDER BY round_idx DESC LIMIT 3"
        ).fetchall()]
        recent_wins = []
        if recent_round_ids:
            placeholders = ",".join("?" for _ in recent_round_ids)
            recent_wins = [dict(row) for row in conn.execute(
                f"SELECT * FROM winning_history WHERE round_idx IN ({placeholders}) ORDER BY round_idx DESC, created_at DESC",
                recent_round_ids
            ).fetchall()]
    return render_template_string(ADMIN_TEMPLATE, devices=connected_devices, device_order=device_order, vault_balance=house_vault, displayed_profit=game_profit, reserve=PROTECTED_RESERVE, cycle_return_pool=cycle_return_pool, pending_payouts=pending, recent_wins=recent_wins, grand_total=grand_total, total_players=total_players, pending_sum=pending_sum, error=request.args.get('error'))

@app.route('/admin/manage', methods=['POST'])
def admin_manage():
    global total_player_deposits
    if not session.get('is_admin') or session.get('device_id') != master_admin_device_id: return redirect(url_for('portal'))
    dev_id, action = request.form.get('device_id'), request.form.get('action')
    try: amount = float(request.form.get('amount'))
    except: return redirect(url_for('admin', error="Invalid amount."))

    if dev_id not in connected_devices or amount <= 0: return redirect(url_for('admin', error="Invalid device/amount."))

    with lock:
        if action == 'add':
            connected_devices[dev_id]["balance"] = round(connected_devices[dev_id]["balance"] + amount, 2)
            total_player_deposits = round(total_player_deposits + amount, 2)
            sync_device_to_db(dev_id)
            save_system_metric('total_player_deposits', total_player_deposits)
            central_expense(amount, f"player-deposit:device:{dev_id}")
            log_system_audit(f"CASHIER ADD +UGX {amount:,.2f}")
        elif action == 'remove':
            if connected_devices[dev_id]["balance"] < amount: return redirect(url_for('admin', error="Balance cannot drop below zero."))
            connected_devices[dev_id]["balance"] = round(connected_devices[dev_id]["balance"] - amount, 2)
            total_player_deposits = round(total_player_deposits - amount, 2)
            sync_device_to_db(dev_id)
            save_system_metric('total_player_deposits', total_player_deposits)
            central_income(amount, f"player-withdrawal:device:{dev_id}")
            log_system_audit(f"CASHIER REMOVE -UGX {amount:,.2f}")
    return redirect(url_for('admin'))

@app.route('/admin/house', methods=['POST'])
def admin_house():
    global house_vault
    if not session.get('is_admin') or session.get('device_id') != master_admin_device_id: return redirect(url_for('portal'))
    action = request.form.get('action')
    try: amount = float(request.form.get('amount'))
    except: return redirect(url_for('admin', error="Invalid amount."))

    if amount <= 0: return redirect(url_for('admin', error="Amount must be positive."))

    with lock:
        if action == 'add':
            house_vault = round(house_vault + amount, 2)
            save_system_metric('house_vault', house_vault)
            central_expense(amount, "house-fund-add")
        elif action == 'remove':
            if house_vault - amount < PROTECTED_RESERVE:
                return redirect(url_for('admin', error=f"Cannot drop below UGX {PROTECTED_RESERVE:,.2f} reserve."))
            house_vault = round(house_vault - amount, 2)
            save_system_metric('house_vault', house_vault)
            central_income(amount, "house-fund-remove")
    return redirect(url_for('admin'))


# ----------------------------------------------------
# AVIATOR GAME (integrated into the website; shares the same device balances)
# Admin access is inherited from the football admin PIN and one-device lock.
# ----------------------------------------------------
AVIATOR_HOUSE_START = 50000.0
aviator_lock = threading.RLock()
aviator_game = {"status": "WAITING", "multiplier": 1.0, "crash_point": 1.0,
                "history": [1.08, 5.23, 1.08, 3.91, 1.28, 1.07, 3.97],
                "message": "PLACE YOUR BETS"}
aviator_players = {}
aviator_house_balance = AVIATOR_HOUSE_START
aviator_net_profit = 0.0
def save_aviator_state():
    """Persist Aviator history/player bet state using the same persistent SQLite DB."""
    try:
        with get_db() as conn:
            payload = {"history": aviator_game.get("history", []), "players": aviator_players}
            conn.execute("INSERT OR REPLACE INTO system_state (key, val_num, val_str) VALUES (?, ?, ?)",
                         ("aviator_state_json", None, json.dumps(payload)))
            conn.commit()
    except Exception:
        pass

try:
    with get_db() as _aviator_conn:
        _house_row = _aviator_conn.execute("SELECT val_num FROM system_state WHERE key='aviator_house_balance'").fetchone()
        _profit_row = _aviator_conn.execute("SELECT val_num FROM system_state WHERE key='aviator_net_profit'").fetchone()
        _state_row = _aviator_conn.execute("SELECT val_str FROM system_state WHERE key='aviator_state_json'").fetchone()
        if _house_row and _house_row[0] is not None: aviator_house_balance = float(_house_row[0])
        if _profit_row and _profit_row[0] is not None: aviator_net_profit = float(_profit_row[0])
        if _state_row and _state_row[0]:
            _saved = json.loads(_state_row[0])
            if isinstance(_saved.get("history"), list): aviator_game["history"] = _saved["history"][:10]
            if isinstance(_saved.get("players"), dict): aviator_players.update(_saved["players"])
            # If a deployment interrupted a live bet, treat charged, uncollected stakes as losses.
            for _ap in aviator_players.values():
                if _ap.get("charged_1") and _ap.get("bet_active_1") and not _ap.get("cashed_out_1"):
                    _ap["last_result"] = "Round interrupted by server restart; stake was lost."
                if _ap.get("charged_2") and _ap.get("bet_active_2") and not _ap.get("cashed_out_2"):
                    _ap["last_result"] = "Round interrupted by server restart; stake was lost."
                _ap["bet_active_1"] = _ap["bet_active_2"] = False
                _ap["cashed_out_1"] = _ap["cashed_out_2"] = False
                _ap["charged_1"] = _ap["charged_2"] = False
except Exception:
    pass

def aviator_player(dev_id):
    if dev_id not in aviator_players:
        aviator_players[dev_id] = {"bet_active_1": False, "stake_1": 500, "cashed_out_1": False,
                                   "bet_active_2": False, "stake_2": 500, "cashed_out_2": False,
                                   "last_result": "Welcome to Aviator.", "charged_1": False, "charged_2": False}
    return aviator_players[dev_id]

def aviator_loop():
    """Run Aviator continuously like the original standalone/mobile version.

    Each cycle opens a 3-second betting window, charges queued stakes exactly
    once at take-off, advances the multiplier, records the crash, and starts
    the next round. Per-round failures are logged with a traceback so the loop
    can recover instead of silently appearing stuck on the waiting screen.
    """
    global aviator_house_balance, aviator_net_profit
    while True:
        try:
            # BETTING WINDOW: clients can queue bets while status is WAITING.
            with aviator_lock:
                aviator_game.update(status="WAITING", multiplier=1.0,
                                    message="NEXT ROUND STARTING SOON...")
                for ap in list(aviator_players.values()):
                    for key, default in (("bet_active_1", False), ("bet_active_2", False),
                                         ("cashed_out_1", False), ("cashed_out_2", False),
                                         ("charged_1", False), ("charged_2", False),
                                         ("stake_1", 500), ("stake_2", 500)):
                        ap.setdefault(key, default)
                    if not ap.get("bet_active_1"):
                        ap["cashed_out_1"] = False
                        ap["charged_1"] = False
                    if not ap.get("bet_active_2"):
                        ap["cashed_out_2"] = False
                        ap["charged_2"] = False
            time.sleep(3.0)

            # Determine this round's crash point and take queued stakes.
            with aviator_lock:
                r = random.random()
                if r < .40:
                    cp = round(random.uniform(1.00, 1.20), 2)
                elif r < .75:
                    cp = round(random.uniform(1.21, 2.50), 2)
                elif r < .95:
                    cp = round(random.uniform(2.51, 10.00), 2)
                else:
                    cp = round(random.uniform(10.01, 50.00), 2)
                aviator_game.update(status="RUNNING", multiplier=1.0,
                                    crash_point=cp, message="PLANE TAKING OFF!")
                for dev_id, ap in list(aviator_players.items()):
                    info = connected_devices.get(dev_id)
                    if not info:
                        ap["bet_active_1"] = ap["bet_active_2"] = False
                        ap["charged_1"] = ap["charged_2"] = False
                        continue
                    with lock:
                        for panel in (1, 2):
                            active_key = f"bet_active_{panel}"
                            charged_key = f"charged_{panel}"
                            if ap.get(active_key) and not ap.get(charged_key) and not ap.get(f"cashed_out_{panel}"):
                                try:
                                    stake = float(ap.get(f"stake_{panel}", 0))
                                except (TypeError, ValueError):
                                    stake = 0.0
                                if 0 < stake <= float(info.get("balance", 0)):
                                    info["balance"] = round(float(info["balance"]) - stake, 2)
                                    aviator_house_balance = round(aviator_house_balance + stake, 2)
                                    aviator_net_profit = round(aviator_net_profit + stake, 2)
                                    ap[charged_key] = True
                                    sync_device_to_db(dev_id)
                                else:
                                    ap[active_key] = False
                                    ap[charged_key] = False
                                    ap["last_result"] = "Bet cancelled: insufficient balance at take-off."
                save_system_metric('aviator_house_balance', aviator_house_balance)
                save_system_metric('aviator_net_profit', aviator_net_profit)
                save_aviator_state()

            # FLIGHT: smoothly increase multiplier until it reaches the crash point.
            current_mult = 1.0
            while current_mult < cp:
                time.sleep(0.08)
                current_mult = round(current_mult + max(0.01, current_mult * 0.03), 2)
                if current_mult > cp:
                    current_mult = cp
                with aviator_lock:
                    aviator_game["multiplier"] = current_mult
                    aviator_game["message"] = f"FLYING — {current_mult:.2f}x"

            # CRASH and clear round bets. Cash-outs already collected stay settled.
            with aviator_lock:
                aviator_game.update(status="CRASHED", multiplier=cp,
                                    message=f"FLEW AWAY @ {cp:.2f}x!")
                history = aviator_game.setdefault("history", [])
                history.insert(0, cp)
                del history[10:]
                for ap in list(aviator_players.values()):
                    for panel in (1, 2):
                        if ap.get(f"bet_active_{panel}") and not ap.get(f"cashed_out_{panel}"):
                            ap["last_result"] = (
                                f"Bet #{panel} crashed @ {cp:.2f}x — lost UGX "
                                f"{int(float(ap.get(f'stake_{panel}', 0))):,}"
                            )
                        ap[f"bet_active_{panel}"] = False
                        ap[f"cashed_out_{panel}"] = False
                        ap[f"charged_{panel}"] = False
                save_aviator_state()
            time.sleep(3.0)
        except Exception as exc:
            # Keep the shared game alive, but make the actual failure visible in Render logs.
            import traceback
            print(f"[AVIATOR LOOP ERROR] {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
            try:
                with aviator_lock:
                    aviator_game.update(status="WAITING", multiplier=1.0,
                                        message="ROUND RECOVERING — NEXT ROUND SOON")
            except Exception:
                pass
            time.sleep(1.0)

# Start only one game loop in the single-worker Render deployment.
if not app.config.get("AVIATOR_LOOP_STARTED"):
    app.config["AVIATOR_LOOP_STARTED"] = True
    threading.Thread(target=aviator_loop, daemon=True, name="aviator-loop").start()

AVIATOR_TEMPLATE = r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Aviator | Virtual Betting Arena</title>
<style>body{margin:0;background:#0f141c;color:#fff;font-family:Arial,sans-serif;padding:12px}main{max-width:500px;margin:auto}.top,.card{background:#182232;border:1px solid #27354f;border-radius:10px;padding:12px;margin-bottom:10px}.top{display:flex;justify-content:space-between;align-items:center}.brand{color:#ef4444;font-weight:bold;font-size:20px}.bal{color:#4ade80;font-weight:bold}.history{display:flex;gap:6px;overflow:auto;margin:10px 0}.chip{background:#202e43;padding:6px 9px;border-radius:6px;white-space:nowrap;color:#4ade80;font-size:12px}.flight{height:190px;background:#090d14;border:2px solid #27354f;border-radius:12px;display:flex;flex-direction:column;align-items:center;justify-content:center;margin-bottom:10px}.mult{font-size:40px;font-weight:bold}.muted{color:#94a3b8;font-size:12px}.panel{background:#182232;border:1px solid #27354f;padding:12px;border-radius:10px;margin-bottom:10px}.line{display:flex;justify-content:space-between;align-items:center;gap:8px}.stake{width:120px;padding:9px;background:#090d14;color:#facc15;border:1px solid #27354f;border-radius:6px;font-size:16px}.quick{display:flex;gap:5px;margin:8px 0}.quick button{flex:1;background:#27354f;color:#fff;border:0;border-radius:5px;padding:8px}.action{width:100%;padding:12px;border:0;border-radius:7px;background:#22c55e;color:#fff;font-size:16px;font-weight:bold}.action.cash{background:#eab308;color:#111}.result{background:#090d14;border:1px dashed #eab308;padding:8px;text-align:center;border-radius:6px;margin:8px 0;font-size:13px}.nav{display:flex;gap:8px}.nav a{flex:1;text-align:center;padding:10px;background:#27354f;border-radius:7px;color:#fff;text-decoration:none;font-size:13px}</style></head><body><main>
<div class="top"><span class="brand">✈ AVIATOR</span><span class="bal" id="bal">UGX {{ '%.0f'|format(balance) }}</span></div>
<div class="nav"><a href="/">Game Lobby</a><a href="/arena">Football</a></div>
<div class="history" id="history"></div><div class="flight"><div style="font-size:27px">✈️</div><div class="mult" id="mult">1.00x</div><div class="muted" id="msg">Waiting for next round</div></div><div class="result" id="result">Welcome to Aviator.</div>
{% for n in [1,2] %}<div class="panel"><div class="line"><b>Bet {{n}} stake (UGX)</b><input class="stake" id="stake{{n}}" type="number" min="1" value="500"></div><div class="quick">{% for a in [500,1000,5000,10000] %}<button onclick="document.getElementById('stake{{n}}').value={{a}}">{{'{:,}'.format(a)}}</button>{% endfor %}</div><button class="action" id="btn{{n}}" onclick="act({{n}})">BET</button></div>{% endfor %}
</main><script>
function money(v){return 'UGX '+Math.floor(Number(v||0)).toLocaleString();}
function act(panel){const b=document.getElementById('btn'+panel);const action=b.dataset.action==='cashout'?'cashout':'bet';const amount=Number(document.getElementById('stake'+panel).value||500);fetch('/aviator/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,panel,amount})}).then(r=>r.json()).then(draw).catch(()=>{});}
function draw(s){document.getElementById('bal').textContent=money(s.balance);document.getElementById('mult').textContent=Number(s.multiplier||1).toFixed(2)+'x';document.getElementById('msg').textContent=s.message;document.getElementById('result').textContent=s.last_result||'';document.getElementById('history').innerHTML=(s.history||[]).map(h=>`<span class="chip">${Number(h).toFixed(2)}x</span>`).join('');[1,2].forEach(n=>{let b=document.getElementById('btn'+n);if(s.status==='RUNNING'&&s['bet_active_'+n]&&!s['cashed_out_'+n]){b.textContent='STOP & COLLECT';b.className='action cash';b.dataset.action='cashout';}else if(s.status==='WAITING'&&s['bet_active_'+n]){b.textContent='BET QUEUED';b.className='action';b.dataset.action='queued';}else{b.textContent='BET';b.className='action';b.dataset.action='bet';}})}
function sync(){fetch('/aviator/state').then(r=>r.json()).then(draw).catch(()=>{});}setInterval(sync,400);sync();
</script></body></html>'''

@app.route('/aviator')
def aviator_page():
    dev_id = session.get('device_id')
    info = connected_devices.get(dev_id, {"balance": 0})
    return render_template_string(AVIATOR_TEMPLATE, balance=info.get("balance", 0))

@app.route('/aviator/state')
def aviator_state():
    dev_id = session.get('device_id')
    info = connected_devices.get(dev_id, {"balance": 0})
    with aviator_lock:
        ap = aviator_player(dev_id)
        return jsonify({**aviator_game, **ap, "balance": info.get("balance", 0)})

@app.route('/aviator/command', methods=['POST'])
def aviator_command():
    global aviator_house_balance, aviator_net_profit
    dev_id = session.get('device_id')
    info = connected_devices.get(dev_id)
    if not info:
        return jsonify({"error": "Device account not found"}), 400
    data = request.get_json(silent=True) or {}
    action = data.get('action')
    try: panel = int(data.get('panel', 1)); amount = float(data.get('amount', 500))
    except (ValueError, TypeError): return jsonify({"error": "Invalid bet"}), 400
    if panel not in (1, 2): return jsonify({"error": "Invalid bet panel"}), 400
    with aviator_lock:
        ap = aviator_player(dev_id)
        if action == 'bet':
            if aviator_game['status'] != 'WAITING': ap['last_result'] = 'Round already started. Wait for the next round.'
            elif amount <= 0 or amount > info['balance']: ap['last_result'] = 'Invalid stake or insufficient balance.'
            elif ap[f'bet_active_{panel}']: ap['last_result'] = f'Bet #{panel} is already queued.'
            else:
                other = 2 if panel == 1 else 1
                already_reserved = ap[f'stake_{other}'] if ap[f'bet_active_{other}'] else 0
                if amount + already_reserved > info['balance']:
                    ap['last_result'] = 'Not enough balance for both bets.'
                else:
                    ap[f'stake_{panel}'] = amount; ap[f'bet_active_{panel}'] = True; ap[f'cashed_out_{panel}'] = False
                    ap['last_result'] = f'Bet #{panel} queued: UGX {amount:,.0f}'
                    save_aviator_state()
        elif action == 'cashout':
            if aviator_game['status'] == 'RUNNING' and ap[f'bet_active_{panel}'] and not ap[f'cashed_out_{panel}']:
                stake = float(ap[f'stake_{panel}'])
                payout = round(stake * aviator_game['multiplier'], 2)
                if not ap.get(f'charged_{panel}', False):
                    ap['last_result'] = 'This bet was not charged at take-off and cannot be collected.'
                    ap[f'bet_active_{panel}'] = False
                else:
                    with lock:
                        info['balance'] = round(info['balance'] + payout, 2)
                        sync_device_to_db(dev_id)
                    aviator_house_balance = round(aviator_house_balance - payout, 2)
                    aviator_net_profit = round(aviator_net_profit - payout, 2)
                    save_system_metric('aviator_house_balance', aviator_house_balance)
                    save_system_metric('aviator_net_profit', aviator_net_profit)
                    ap[f'cashed_out_{panel}'] = True
                    ap[f'charged_{panel}'] = False
                    ap['last_result'] = f'Collected at {aviator_game["multiplier"]:.2f}x: {payout:,.0f} UGX'
                    save_aviator_state()
            else: ap['last_result'] = 'Cannot collect this bet now.'
        else: ap['last_result'] = 'Unknown action.'
        info = connected_devices.get(dev_id, {"balance": 0})
        return jsonify({**aviator_game, **ap, "balance": info.get("balance", 0)})

@app.route('/aviator/admin')
def aviator_admin():
    dev_id = session.get('device_id')
    if not session.get('is_admin') or dev_id != master_admin_device_id:
        return redirect(url_for('portal', error='Enter PIN 4422 on the registered admin device first.'))
    with aviator_lock:
        rows = []
        for pid, ap in aviator_players.items():
            d = connected_devices.get(pid, {})
            rows.append((d.get('number', '?'), d.get('balance', 0), ap.get('last_result', '')))
    return render_template_string('''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Aviator Admin</title><style>body{background:#101820;color:white;font-family:Arial;padding:20px}.card{max-width:700px;margin:auto;background:#1c2a39;padding:20px;border-radius:12px}a{color:#6ee7b7}td,th{padding:8px;border-bottom:1px solid #405166;text-align:left}</style></head><body><div class="card"><h2>✈ Aviator Admin</h2><p>Access is restricted to the same registered device as Football Admin and requires PIN 4422.</p><p>House balance: <b>UGX {{'%.2f'|format(house)}}</b></p><p>Net house profit/loss: <b>UGX {{'%.2f'|format(profit)}}</b></p><p>Round: <b>{{status}}</b> — {{message}}</p><table><tr><th>Device</th><th>Balance</th><th>Last Aviator result</th></tr>{% for n,b,r in rows %}<tr><td>{{n}}</td><td>UGX {{'%.2f'|format(b)}}</td><td>{{r}}</td></tr>{% endfor %}</table><p><a href="/admin">Football Admin</a> · <a href="/">Lobby</a></p></div></body></html>''', house=aviator_house_balance, profit=aviator_net_profit, status=aviator_game['status'], message=aviator_game['message'], rows=rows)


# ----------------------------------------------------
# RUGBY DEMO GAME — separate selection, shared player balance
# ----------------------------------------------------
RUGBY_CYCLE_SECONDS = 45
RUGBY_BETTING_SECONDS = 15
rugby_lock = threading.RLock()
rugby_bets = {}
rugby_settled = set()
RUGBY_TEAMS = [("Uganda Cranes", "Kenya Simbas"), ("South Africa", "New Zealand"),
               ("England", "Ireland"), ("France", "Wales"), ("Australia", "Fiji")]

def rugby_round_data():
    now = time.time()
    idx = int(now // RUGBY_CYCLE_SECONDS)
    elapsed = now % RUGBY_CYCLE_SECONDS
    rng = random.Random(87000 + idx)
    home, away = RUGBY_TEAMS[rng.randrange(len(RUGBY_TEAMS))]
    # Fixed result per round, shared by all players.
    home_score = rng.choice([7, 12, 14, 17, 19, 21, 24, 28, 31, 35])
    away_score = rng.choice([0, 5, 7, 10, 14, 17, 21, 24, 28, 33])
    if home_score > away_score: result = "1"
    elif home_score < away_score: result = "2"
    else: result = "X"
    phase = "BETTING" if elapsed < RUGBY_BETTING_SECONDS else "LIVE"
    # Settle the previous round once everyone has moved into the next round.
    previous = idx - 1
    if previous >= 0 and previous not in rugby_settled:
        with rugby_lock:
            if previous not in rugby_settled:
                old_rng = random.Random(87000 + previous)
                old_rng.randrange(len(RUGBY_TEAMS))
                old_home = old_rng.choice([7, 12, 14, 17, 19, 21, 24, 28, 31, 35])
                old_away = old_rng.choice([0, 5, 7, 10, 14, 17, 21, 24, 28, 33])
                outcome = "1" if old_home > old_away else ("2" if old_home < old_away else "X")
                for bet in rugby_bets.get(previous, []):
                    if not bet.get("settled"):
                        bet["settled"] = True
                        if bet["selection"] == outcome:
                            dev = connected_devices.get(bet["dev_id"])
                            if dev:
                                dev["balance"] = round(dev["balance"] + bet["stake"] * bet["odds"], 2)
                                sync_device_to_db(bet["dev_id"])
                rugby_settled.add(previous)
    return {"round": idx, "phase": phase, "seconds_left": round(max(0, (RUGBY_BETTING_SECONDS if phase == "BETTING" else RUGBY_CYCLE_SECONDS) - elapsed), 1),
            "home": home, "away": away, "home_score": home_score if phase == "LIVE" else 0,
            "away_score": away_score if phase == "LIVE" else 0, "result": result if phase == "LIVE" else None,
            "odds": {"1": 1.90, "X": 14.0, "2": 2.10}}

RUGBY_TEMPLATE = r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Rugby | Virtual Betting Arena</title><style>
body{margin:0;background:#08140f;color:#fff;font-family:Arial;padding:12px}main{max-width:600px;margin:auto}.card{background:#10251b;border:1px solid #2f5940;border-radius:12px;padding:14px;margin-bottom:12px}.top{display:flex;justify-content:space-between;gap:10px}.balance{color:#86efac;font-weight:bold}.field{height:190px;border:2px solid #dcfce7;border-radius:10px;background:repeating-linear-gradient(0deg,#166534 0 35px,#14532d 35px 70px);display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center}.score{font-size:38px;font-weight:bold;margin:12px}.options{display:grid;grid-template-columns:repeat(3,1fr);gap:7px}.options button,button{border:0;border-radius:7px;padding:12px;font-weight:bold;cursor:pointer}.options button{background:#244633;color:white}.options button span{display:block;color:#bbf7d0;margin-top:5px}.stake{width:100%;box-sizing:border-box;background:#07110b;color:#fff;border:1px solid #3d6a4d;padding:12px;border-radius:7px;margin:8px 0}.go{width:100%;background:#22c55e;color:#06240e}.nav{color:#bbf7d0;text-decoration:none;margin-right:12px}.note{color:#bbd9c3;font-size:13px}</style></head><body><main><div class="card top"><b>🏉 VIRTUAL RUGBY</b><span class="balance" id="balance">UGX {{balance}}</span></div><div class="card"><a class="nav" href="/">Game Lobby</a><a class="nav" href="/arena">Football</a><a class="nav" href="/aviator">Aviator</a></div><div class="field card"><div id="phase">Loading round…</div><div id="teams">Preparing teams</div><div class="score" id="score">VS</div><div id="clock">--</div></div><div class="card"><b>Match winner (1X2)</b><div class="options" style="margin-top:10px"><button onclick="choose('1')" id="o1">HOME<span>1.90x</span></button><button onclick="choose('X')" id="oX">DRAW<span>14.00x</span></button><button onclick="choose('2')" id="o2">AWAY<span>2.10x</span></button></div><input class="stake" id="stake" type="number" min="100" value="500"/><button class="go" onclick="placeBet()">PLACE RUGBY BET</button><p class="note" id="message">Demo match. Bets close when the simulated match begins.</p></div></main><script>
let selected='1',roundNow=-1;function choose(x){selected=x;['1','X','2'].forEach(k=>document.getElementById('o'+k).style.outline=k===x?'2px solid #86efac':'none')}
async function refresh(){try{let s=await(await fetch('/rugby/state')).json();document.getElementById('balance').textContent='UGX '+Math.floor(s.balance).toLocaleString();document.getElementById('phase').textContent=s.phase==='BETTING'?'BETTING OPEN':'MATCH IN PLAY';document.getElementById('teams').textContent=s.home+' vs '+s.away;document.getElementById('score').textContent=s.phase==='LIVE'?s.home_score+' - '+s.away_score:'VS';document.getElementById('clock').textContent=s.seconds_left+' seconds '+(s.phase==='BETTING'?'to bet':'remaining in round');if(s.round!==roundNow){roundNow=s.round;document.getElementById('message').textContent='New rugby round. Choose a winner and place your stake.'}['1','X','2'].forEach(k=>document.getElementById('o'+k).disabled=s.phase!=='BETTING');}catch(e){}}
async function placeBet(){let stake=Number(document.getElementById('stake').value);try{let r=await(await fetch('/rugby/bet',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selection:selected,stake})})).json();document.getElementById('message').textContent=r.message;refresh()}catch(e){document.getElementById('message').textContent='Connection error; try again.'}}
setInterval(refresh,1000);refresh();</script></body></html>'''

@app.route('/rugby')
def rugby_page():
    dev_id = session.get('device_id')
    info = connected_devices.get(dev_id, {"balance": 0})
    return render_template_string(RUGBY_TEMPLATE, balance=f"{info.get('balance', 0):,.0f}")

@app.route('/rugby/state')
def rugby_state():
    dev_id = session.get('device_id')
    data = rugby_round_data()
    data["balance"] = connected_devices.get(dev_id, {}).get("balance", 0)
    return jsonify(data)

@app.route('/rugby/bet', methods=['POST'])
def rugby_bet():
    dev_id = session.get('device_id')
    info = connected_devices.get(dev_id)
    if not info: return jsonify({"success": False, "message": "Player account not found."}), 400
    data = request.get_json(silent=True) or {}
    try: stake = round(float(data.get('stake', 0)), 2)
    except (TypeError, ValueError): stake = 0
    selection = data.get('selection')
    if stake < 100 or stake > 50000: return jsonify({"success": False, "message": "Stake must be between UGX 100 and UGX 50,000."})
    if selection not in ("1", "X", "2"): return jsonify({"success": False, "message": "Choose home, draw or away."})
    with rugby_lock:
        rd = rugby_round_data()
        if rd["phase"] != "BETTING": return jsonify({"success": False, "message": "Betting is closed for this round."})
        with lock:
            if info["balance"] < stake: return jsonify({"success": False, "message": "Insufficient balance."})
            info["balance"] = round(info["balance"] - stake, 2)
            sync_device_to_db(dev_id)
        rugby_bets.setdefault(rd["round"], []).append({"dev_id": dev_id, "stake": stake, "selection": selection, "odds": rd["odds"][selection], "settled": False})
    return jsonify({"success": True, "message": f"Rugby bet placed: UGX {stake:,.0f} on {selection}."})

PORTAL_TEMPLATE = r"""
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Virtual Betting Arena</title><style>
*{box-sizing:border-box}body{margin:0;background:#07152b;color:#f7fafc;font-family:Arial,sans-serif}.top{background:#0c203c;border-bottom:2px solid #1b8f59;padding:12px 16px;display:flex;align-items:center;justify-content:space-between;gap:10px;position:sticky;top:0;z-index:5}.logo{font-weight:900;color:#35d780;font-size:20px}.bal{background:#123c31;color:#9fffc5;padding:9px 12px;border-radius:9px;font-weight:900;white-space:nowrap}.wrap{max-width:1000px;margin:auto;padding:14px}.welcome{background:linear-gradient(115deg,#12355b,#10452f);padding:18px;border-radius:14px;border:1px solid #285c68;margin-bottom:14px}.welcome h1{margin:0 0 6px;font-size:22px}.muted{color:#b5c7dc;font-size:13px}.notice{background:#123e2c;color:#a7f3d0;border-radius:8px;padding:10px;margin:8px 0}.err{background:#4b1d2b;color:#ffc4cf;border-radius:8px;padding:10px;margin:8px 0}.section-title{font-size:16px;font-weight:900;margin:20px 0 10px}.games{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.game{display:flex;gap:12px;align-items:center;text-decoration:none;color:#fff;background:#102542;border:1px solid #27486e;border-radius:12px;padding:14px;min-height:100px}.game:hover{border-color:#37d783;transform:translateY(-1px)}.emoji{font-size:32px;width:45px;text-align:center}.game b{display:block;margin-bottom:5px}.tag{display:inline-block;color:#8df0b4;font-size:11px;margin-top:4px}.wallet{background:#102542;border:1px solid #27486e;border-radius:12px;padding:16px;margin-top:14px}.formgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.field{width:100%;padding:12px;background:#07172e;color:white;border:1px solid #365779;border-radius:8px;margin-top:5px}.submit{background:#2bd178;color:#062014;font-weight:900;border:0;border-radius:8px;padding:12px;cursor:pointer}.links{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}.links a{color:#a7f3d0}.small{font-size:12px;color:#9fb4ce}@media(max-width:520px){.games{grid-template-columns:1fr}.formgrid{grid-template-columns:1fr}.logo{font-size:16px}.top{padding:10px}}
</style></head><body><header class="top"><div class="logo">⚡ VIRTUAL BETTING ARENA</div><div class="bal">UGX {{'%.0f'|format(balance)}}</div></header><main class="wrap"><section class="welcome"><h1>Welcome, {{account_name}}</h1><div class="muted">One account · One wallet · Play any game and keep the same balance everywhere.</div></section>{% if request.args.get('error') %}<div class="err">{{request.args.get('error')}}</div>{% endif %}{% if request.args.get('deposit_error') %}<div class="err">{{request.args.get('deposit_error')}}</div>{% endif %}{% if request.args.get('deposit_message') %}<div class="notice">{{request.args.get('deposit_message')}}</div>{% endif %}<div class="section-title">POPULAR GAMES</div><div class="games"><a class="game" href="/arena"><span class="emoji">⚽</span><span><b>Virtual Football</b><span class="muted">Full-time & half-time 1X2</span><span class="tag">PLAY NOW →</span></span></a><a class="game" href="/aviator"><span class="emoji">✈️</span><span><b>Aviator</b><span class="muted">Watch the multiplier and cash out</span><span class="tag">PLAY NOW →</span></span></a><a class="game" href="/rugby"><span class="emoji">🏉</span><span><b>Rugby</b><span class="muted">Pick the match result</span><span class="tag">PLAY NOW →</span></span></a><a class="game" href="/velocity"><span class="emoji">🏎️</span><span><b>Velocity Car Racing</b><span class="muted">Choose your winning car</span><span class="tag">PLAY NOW →</span></span></a><a class="game" href="/chicken-clash"><span class="emoji">🐓</span><span><b>Chicken Clash</b><span class="muted">Battle arena</span><span class="tag">PLAY NOW →</span></span></a><a class="game" href="/hot-7-fruit"><span class="emoji">🍒</span><span><b>Hot 7 Fruit</b><span class="muted">Fruit reel lines</span><span class="tag">SPIN NOW →</span></span></a><a class="game" href="/fortune-slots"><span class="emoji">🎰</span><span><b>Fortune Slots</b><span class="muted">Spin the reels</span><span class="tag">SPIN NOW →</span></span></a></div><section class="wallet"><div class="section-title" style="margin-top:0">MY WALLET</div><p class="muted">Request a deposit after sending payment. The admin credits your wallet only after verifying the reference.</p><form method="post" action="/deposit/request"><div class="formgrid"><label>Amount (UGX)<input class="field" type="number" name="amount" min="1000" max="10000000" step="1" required placeholder="e.g. 5000"></label><label>Payment method<input class="field" name="method" required maxlength="40" placeholder="e.g. Mobile Money"></label><label style="grid-column:1/-1">Payment reference / transaction ID<input class="field" name="reference" required maxlength="120" placeholder="Enter the payment reference from your provider"></label></div><button class="submit" style="margin-top:12px;width:100%">SUBMIT DEPOSIT REQUEST</button></form><div class="small" style="margin-top:10px">This package does not yet connect to an automatic mobile-money payment gateway. Verify payments before approving a deposit.</div></section><section class="wallet"><div class="section-title" style="margin-top:0">RECENT DEPOSIT REQUESTS</div>{% for d in deposit_requests %}<div style="padding:9px 0;border-bottom:1px solid #27486e"><b>UGX {{'%.0f'|format(d.amount)}}</b> · {{d.method}} · {{d.status|upper}}<div class="small">Reference: {{d.payment_reference}} · {{d.created_at|int}}</div></div>{% else %}<div class="small">No deposit requests yet.</div>{% endfor %}</section><section class="wallet"><div class="section-title" style="margin-top:0">OWNER / ADMIN</div><form method="post" action="/admin-auth"><label>Admin PIN<input class="field" type="password" name="pin" inputmode="numeric" maxlength="4" required placeholder="Enter admin PIN"></label><button class="submit" style="margin-top:8px;width:100%">OPEN ADMIN PANEL</button></form><div class="small" style="margin-top:8px">The admin panel is locked to the first registered admin account that claims it with the PIN.</div></section><div class="links"><a href="/logout">Log out</a><a href="/admin/deposits">Deposit administration</a><a href="/admin">Admin panel</a></div><p class="small">Account wallet is shared by all games. Game stakes reduce the wallet; confirmed winnings are returned to the same wallet.</p></main></body></html>
"""

MAIN_TEMPLATE = """
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>JJ Virtual Football Arena</title><style>
body{font-family:Arial;background:#07111c;color:#fff;margin:0;padding-bottom:150px}
header{background:#101d2c;padding:11px 15px;display:flex;justify-content:space-between;border-bottom:2px solid {{color}};align-items:center;position:sticky;top:0;z-index:10}
.logo{color:{{color}};font-weight:bold;font-size:.95rem}.balance{background:#1c2b3c;padding:7px 13px;border-radius:16px;color:#49d7ff;font-weight:800}
.container{padding:10px;max-width:680px;margin:auto}.card{background:#0e1b29;border:1px solid #25415d;border-radius:10px;padding:12px;margin-bottom:10px}
.title{font-size:.9rem;font-weight:bold;color:#dcecff;margin:10px 0 7px;text-align:left;border-left:3px solid {{color}};padding-left:7px}
.grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}.odd{background:#18293b;color:#d7e5f3;border:1px solid #34516e;border-radius:6px;padding:9px;font-size:.8rem;cursor:pointer}
.odd.selected{background:{{color}};color:#001018;font-weight:bold;border-color:{{color}}}
.input{width:100%;box-sizing:border-box;padding:10px;background:#142333;color:#fff;border:1px solid #34516e;border-radius:5px;margin-top:6px}
.place{width:100%;padding:12px;margin-top:8px;background:{{color}};border:0;border-radius:5px;font-weight:bold;color:#001018;cursor:pointer}.place:disabled{background:#303d4b;color:#9aa8b5}
.bet{position:fixed;bottom:0;left:0;width:100%;box-sizing:border-box;background:#0d1825;border-top:2px solid {{color}};padding:11px;z-index:20}
#resultBox{display:none}.win{background:#0c3b29;border:1px solid #28c77a;color:#78f0b3}.loss{background:#401b20;border:1px solid #ef5364;color:#ff9ca6}.pending{background:#26364a;border:1px solid #56738f;color:#b9d4ec}
.live{display:none;background:#050a10;position:fixed;inset:0;z-index:30;padding:8px;overflow-y:auto}
.tv-header{background:#111b27;border:1px solid #304a64;border-radius:6px;padding:8px 12px;display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.tv-teams{font-weight:bold;font-size:.95rem;color:#f0f6fc}.tv-status{font-size:.75rem;color:#9bb1c7}
.tv-scorebox{background:#182635;border:1px solid #3a5874;padding:4px 14px;border-radius:4px;font-size:1.4rem;font-weight:bold;color:#58cfff;letter-spacing:2px}
.pitch{height:430px;background:radial-gradient(ellipse at center,#238636 0%,#196c2e 70%,#0e4421 100%);border:3px solid #f0f6fc;border-radius:6px;position:relative;overflow:hidden;margin-bottom:8px;box-shadow:inset 0 0 40px rgba(0,0,0,.6)}
.pitch-lines{position:absolute;inset:0;pointer-events:none}.pitch-lines::before{content:'';position:absolute;top:0;bottom:0;left:50%;width:2px;background:rgba(255,255,255,.65);transform:translateX(-50%)}.pitch-lines::after{content:'';position:absolute;top:50%;left:50%;width:78px;height:78px;border:2px solid rgba(255,255,255,.65);border-radius:50%;transform:translate(-50%,-50%)}
.goal-left{position:absolute;left:0;top:31%;bottom:31%;width:22px;border:3px solid rgba(255,255,255,.9);border-left:0;background:repeating-linear-gradient(0deg,transparent 0 7px,rgba(255,255,255,.25) 8px 9px)}
.goal-right{position:absolute;right:0;top:31%;bottom:31%;width:22px;border:3px solid rgba(255,255,255,.9);border-right:0;background:repeating-linear-gradient(0deg,transparent 0 7px,rgba(255,255,255,.25) 8px 9px)}
.ball{position:absolute;width:13px;height:13px;background:#fff;border-radius:50%;transform:translate(-50%,-50%);transition:all .45s ease-in-out;box-shadow:0 0 10px #fff;z-index:6;border:1px solid #333}
.pitch-clock{position:absolute;top:10px;left:50%;transform:translateX(-50%);z-index:8;background:#07111de8;border:1px solid #7bdcff;border-radius:6px;padding:6px 14px;font-size:18px;font-weight:900;color:#fff;letter-spacing:1px}
.pitch-label{position:absolute;bottom:10px;left:50%;transform:translateX(-50%);z-index:8;background:#07111de8;border:1px solid #35516a;border-radius:6px;padding:5px 10px;font-size:11px;color:#c7d8e8}
#liveResult{display:none;margin-bottom:8px}
</style></head><body>
<header><a class="logo" href="/">⚡ {{session.get('username', 'PLAYER')}}</a><div class="balance" id="bal">UGX {{ "%.2f"|format(balance) }}</div></header>
<div class="container">
<div class="card" id="timer">Loading...</div>
<div class="card" id="resultBox"></div>
<div class="card"><div id="match" style="font-weight:bold;font-size:1.05rem;margin-bottom:10px;color:#58cfff">Loading Match...</div>
  <div class="title">FULL TIME RESULT (1X2)</div>
  <div class="grid">
    <button class="odd market-option" id="ft_1" onclick="pickMarket('ft_result','1')">1 (Home)<br><span id="o1">-</span></button>
    <button class="odd market-option" id="ft_X" onclick="pickMarket('ft_result','X')">X (Draw)<br><span id="ox">-</span></button>
    <button class="odd market-option" id="ft_2" onclick="pickMarket('ft_result','2')">2 (Away)<br><span id="o2">-</span></button>
  </div>
  <div class="title">HALF TIME RESULT (1X2)</div>
  <div class="grid">
    <button class="odd market-option" id="ht_1" onclick="pickMarket('ht_result','1')">HT 1 (Home)<br><span id="ht_o1">-</span></button>
    <button class="odd market-option" id="ht_X" onclick="pickMarket('ht_result','X')">HT X (Draw)<br><span id="ht_ox">-</span></button>
    <button class="odd market-option" id="ht_2" onclick="pickMarket('ht_result','2')">HT 2 (Away)<br><span id="ht_o2">-</span></button>
  </div>
</div>
</div>
<div class="bet">
  <div id="sel" style="font-size:.85rem;color:#8fa7bc;margin-bottom:4px">No selection made</div>
  <input id="stake" class="input" type="number" min="100" max="50000" placeholder="Stake UGX (100 - 50,000)" oninput="calc()">
  <div id="pay" style="color:{{color}};margin-top:4px;font-size:.9rem">Potential Payout: UGX 0.00</div>
  <button id="place" class="place" onclick="bet()">PLACE BET</button>
</div>
<div id="live" class="live">
  <div id="liveResult"></div>
  <div class="tv-header">
    <div><div id="tv-teams" class="tv-teams">Home vs Away</div><div id="tv-status" class="tv-status">Live Match Stream</div></div>
    <div id="tv-score" class="tv-scorebox">0 - 0</div>
  </div>
  <div class="pitch">
    <div id="pitchClock" class="pitch-clock">00:00</div>
    <div class="pitch-lines"></div>
    <div class="goal-left"></div><div class="goal-right"></div>
    <div id="ball" class="ball" style="left:50%;top:50%"></div>
    <div class="pitch-label">LIVE VIRTUAL FOOTBALL BROADCAST</div>
  </div>
</div>
<script>
const id="{{device_id}}";
let sel=null, odds={}, htOdds={}, round=-1, myRoundBets=0, lastResultShown=-1;
setInterval(sync,1000);

function sync(){
 fetch('/get_state').then(r=>{if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}).then(d=>{
   document.getElementById('bal').innerText='UGX '+Number(d.balance||0).toFixed(2);
   if(d.round_idx!==round){
     round=d.round_idx;myRoundBets=0;sel=null;
     document.querySelectorAll('.market-option').forEach(x=>x.classList.remove('selected'));
     document.getElementById('stake').value='';
     document.getElementById('sel').innerText='No selection made';
     document.getElementById('pay').innerText='Potential Payout: UGX 0.00';
   }
   myRoundBets=Array.isArray(d.current_player_bets)?d.current_player_bets.length:0;
   odds=d.odds||{};htOdds=d.ht_odds||{};
   const home=d.home||'Home',away=d.away||'Away';
   document.getElementById('match').innerText=home+' vs '+away;
   document.getElementById('o1').innerText=odds['1']??'-';
   document.getElementById('ox').innerText=odds['X']??'-';
   document.getElementById('o2').innerText=odds['2']??'-';
   document.getElementById('ht_o1').innerText=htOdds['1']??'-';
   document.getElementById('ht_ox').innerText=htOdds['X']??'-';
   document.getElementById('ht_o2').innerText=htOdds['2']??'-';

   if(d.last_player_summary && d.last_player_summary.round_idx!==lastResultShown){
     lastResultShown=d.last_player_summary.round_idx;
     showPlayerResult(d.last_player_summary);
   }

   const t=document.getElementById('timer'),p=document.getElementById('place');
   if(d.phase==='betting'){
     document.getElementById('live').style.display='none';
     t.innerText='⏱️ BETTING OPEN — CLOSES IN '+Math.ceil(d.time_left)+'s';
     p.disabled=false;p.innerText=myRoundBets>0?'PLACE ANOTHER BET':'PLACE BET';
   }else{
     t.innerText=d.half_time_break?'⏸️ HALF-TIME BREAK':'🔴 LIVE MATCH — BETTING CLOSED';
     p.disabled=true;p.innerText='BETTING CLOSED';
     live(d);
   }
 }).catch(err=>console.warn('Sync unavailable:',err));
}

function showPlayerResult(r){
 const box=document.getElementById('resultBox');
 box.style.display='block';
 if(r.wins>0 && r.losses>0) box.className='card pending';
 else if(r.wins>0) box.className='card win';
 else box.className='card loss';
 let title=r.wins>0&&r.losses===0?'✅ YOU WON':r.losses>0&&r.wins===0?'❌ YOU LOST':'📊 ROUND SETTLED';
 let detail='Match '+r.round_idx+' • '+r.home+' '+r.final_home_goals+' - '+r.final_away_goals+' '+r.away;
 let money='Stake: UGX '+r.total_stake.toFixed(2)+' • Payout: UGX '+r.total_payout.toFixed(2)+' • Net: UGX '+r.net.toFixed(2);
 box.innerHTML='<b>'+title+'</b><br>'+detail+'<br>'+money;
}

function getMarketName(market,type){
 const names={ft_result:{1:'FT: Home (1)',X:'FT: Draw (X)',2:'FT: Away (2)'},ht_result:{1:'HT: Home (1)',X:'HT: Draw (X)',2:'HT: Away (2)'}};
 return (names[market]&&names[market][type])||type;
}
function pickMarket(market,type){
 sel={market,type};document.querySelectorAll('.market-option').forEach(b=>b.classList.remove('selected'));
 const btn=document.getElementById((market==='ht_result'?'ht_':'ft_')+type);if(btn)btn.classList.add('selected');
 const source=market==='ht_result'?htOdds:odds;
 document.getElementById('sel').innerText='Selected: '+getMarketName(market,type)+' @ '+(source[type]||0);
 calc();
}
function calc(){
 const s=parseFloat(document.getElementById('stake').value||0),source=sel&&sel.market==='ht_result'?htOdds:odds,o=sel?(source[sel.type]||0):0;
 document.getElementById('pay').innerText='Potential Payout: UGX '+(sel?(s*o):0).toFixed(2);
}
let busy=false;
async function bet(){
 if(busy)return;
 const s=Number(document.getElementById('stake').value);
 if(!sel)return alert('Please pick a Full Time or Half Time market first.');
 if(!Number.isFinite(s)||s<100||s>50000)return alert('Stake must be between UGX 100 and UGX 50,000.');
 busy=true;const b=document.getElementById('place');b.disabled=true;b.innerText='SENDING BET...';
 try{
   const response=await fetch('/place_bet',{method:'POST',headers:{'Content-Type':'application/json','Accept':'application/json'},credentials:'same-origin',body:JSON.stringify({device_id:id,stake:s,selection:{market:sel.market,type:sel.type}})});
   const d=await response.json().catch(()=>({}));
   if(!response.ok)throw new Error(d.message||('Server HTTP '+response.status));
   if(!d.success)throw new Error(d.message||'Bet was not accepted.');
   myRoundBets++;
   document.getElementById('bal').innerText='UGX '+Number(d.new_balance||0).toFixed(2);
   document.getElementById('stake').value='';sel=null;
   document.querySelectorAll('.market-option').forEach(x=>x.classList.remove('selected'));
   document.getElementById('sel').innerText='Bet placed. You can place another bet.';
   document.getElementById('pay').innerText='Potential Payout: UGX 0.00';
   b.innerText='PLACE ANOTHER BET';
 }catch(e){alert(e.message||'Bet could not be placed.');}
 finally{busy=false;b.disabled=false;}
}
function live(d){
 const v=document.getElementById('live');v.style.display='block';
 const elapsed=Math.max(0,Number(d.match_elapsed||0));
 let minute,clock;
 if(elapsed<45){minute=Math.min(45,Math.floor(elapsed/45*45));clock=String(minute).padStart(2,'0')+':00';}
 else if(elapsed<48){minute=45;clock='45:00';}
 else{minute=Math.min(90,45+Math.floor((elapsed-48)/45*45));clock=String(minute).padStart(2,'0')+':00';}
 document.getElementById('pitchClock').innerText=clock;
 document.getElementById('tv-teams').innerText=(d.home||'Home')+' vs '+(d.away||'Away');
 document.getElementById('tv-status').innerText=d.half_time_break?'HALF-TIME BREAK':'LIVE MATCH STREAM • '+minute+"'";
 let hg=0,ag=0;
 if(Array.isArray(d.match_events))d.match_events.filter(e=>e.minute<=minute).forEach(e=>{if(e.type==='goal'){if(e.side==='home')hg++;else if(e.side==='away')ag++;}});
 document.getElementById('tv-score').innerText=hg+' - '+ag;
 const bx=50+Math.sin(elapsed*.9)*38,by=50+Math.cos(elapsed*.73)*30;
 document.getElementById('ball').style.left=bx+'%';document.getElementById('ball').style.top=by+'%';
}
sync();
</script></body></html>
"""


ADMIN_TEMPLATE = """
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Master Admin</title><style>
body{font-family:Arial;background:#121212;color:#fff;padding:15px}.wrap{max-width:800px;margin:auto}.card{background:#1e1e1e;border:1px solid #333;border-radius:8px;padding:18px;margin-bottom:15px}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.stat{background:#252525;padding:12px;text-align:center;border-radius:6px}.v{font-size:1.15rem;font-weight:bold;color:#28a745;margin-top:5px}
input,select{width:100%;box-sizing:border-box;padding:10px;background:#2c2c2c;color:#fff;border:1px solid #444;border-radius:5px;margin:5px 0 12px}
button{padding:11px;border:0;border-radius:5px;font-weight:bold;cursor:pointer}.add{background:#28a745}.remove{background:#dc3545;color:#fff}.row{background:#252525;padding:10px;border-radius:5px;margin:7px 0}
.ok{color:#28a745}.warn{color:#ffc107}.err{color:#dc3545}
.audit-box{background:#0d1117;border:1px solid #238636;border-radius:6px;padding:12px;margin-top:10px}
</style></head><body><div class="wrap"><a href="/" style="color:#28a745">← Portal</a>
<div class="card"><h2>⚙️ Central Arena Control</h2><div class="grid">
<div class="stat">Game Profit (20%)<div class="v">UGX {{ "%.2f"|format(displayed_profit) }}</div></div>
<div class="stat">House Vault<div class="v">UGX {{ "%.2f"|format(vault_balance) }}</div></div>
<div class="stat">Player Return Pool (80%)<div class="v">UGX {{ "%.2f"|format(cycle_return_pool) }}</div></div>
</div><p>Protected reserve: <b>UGX {{ "%.2f"|format(reserve) }}</b></p>
<div class="audit-box">
  <h4 style="margin:0 0 8px;color:#28a745">🔍 System Balance Auditor</h4>
  <div>Players Combined: <b>UGX {{ "%.2f"|format(total_players) }}</b></div>
  <div>Game Profit: <b>UGX {{ "%.2f"|format(displayed_profit) }}</b></div>
  <div>Return Pool: <b>UGX {{ "%.2f"|format(cycle_return_pool) }}</b></div>
  {% if pending_sum > 0 %}<div>Pending Payouts: <b>UGX {{ "%.2f"|format(pending_sum) }}</b></div>{% endif %}
  <hr style="border-color:#333">
  <div><b>Total System Liquidity: UGX {{ "%.2f"|format(grand_total) }}</b></div>
</div>
</div>
<div class="card"><h3>💳 Deposit Requests</h3><p>Review player-submitted payment references and credit the same wallet after verification.</p><a href="/admin/deposits" style="display:inline-block;padding:10px 14px;background:#28a745;color:#07111c;border-radius:6px;text-decoration:none;font-weight:bold">OPEN DEPOSIT REQUESTS</a></div>
<div class="card"><h3>👤 Player Cashier</h3>{% if error %}<p class="err">{{error}}</p>{% endif %}
<form method="POST" action="/admin/manage"><select name="device_id">{% for d in device_order %}{% set x=devices[d] %}<option value="{{d}}">Device {{x.number}} | UGX {{'%.2f'|format(x.balance)}}</option>{% endfor %}</select>
<input name="amount" type="number" min="1" step="any" placeholder="Amount UGX"><button name="action" value="add" class="add">➕ ADD MONEY</button> <button name="action" value="remove" class="remove">➖ REMOVE MONEY</button></form></div>
<div class="card"><h3>🏦 House Funds</h3><form method="POST" action="/admin/house"><input name="amount" type="number" min="1" step="any" placeholder="Amount UGX"><button name="action" value="add" class="add">ADD HOUSE FUNDS</button> <button name="action" value="remove" class="remove">REMOVE HOUSE FUNDS</button></form></div>
<div class="card"><h3>📱 Connected Devices ({{device_order|length}})</h3>{% for d in device_order %}{% set x=devices[d] %}<div class="row">Device {{x.number}} — UGX {{'%.2f'|format(x.balance)}} <small>{{d[:12]}}...</small></div>{% endfor %}</div>
<div class="card"><h3>🎮 Dynamic Return Pool Engine</h3>
<p class="ok">Game Profit automatically receives 20% commission on every bet. All wins are paid exclusively out of the 80% Return Pool on a balanced weighted-probability basis.</p>
</div>
<div class="card"><h3>✅ Payout Safety Monitor</h3>
{% if pending_payouts %}
<p class="warn"><b>{{ pending_payouts|length }}</b> payout(s) pending central delivery.</p>
{% for p in pending_payouts %}
<div class="row">
<b>Round {{p.round_idx}}</b> — Device {{ devices[p.dev_id].number if p.dev_id in devices else p.dev_id[:12] }}<br>
Amount: <b>UGX {{ "%.2f"|format(p.amount) }}</b> — Attempts: {{p.attempts}}<br>
<small>{{p.last_error}}</small>
<form method="POST" action="/admin/payout/retry/{{p.id}}" style="margin-top:8px"><button class="add" type="submit">RETRY PAYOUT</button></form>
</div>
{% endfor %}
{% else %}
<p class="ok">No pending payouts.</p>
{% endif %}
</div>
<div class="card"><h3>🏆 Last 3 Match Results — Winning Bets Only</h3>
{% if recent_wins %}
{% for w in recent_wins %}
<div class="row">
<b>Match {{w.round_idx}}</b> — {{ 'Half-time' if w.market == 'ht_result' else 'Full-time' }} win<br>
Winning side: <b>{{ 'Home' if w.selection_type == '1' else ('Draw' if w.selection_type == 'X' else ('Away' if w.selection_type == '2' else w.selection_type)) }}</b><br>
Winning odds: <b>{{ "%.2f"|format(w.odds) }}x</b> — Payout: <b class="ok">UGX {{ "%.2f"|format(w.payout) }}</b>
</div>
{% endfor %}
{% else %}
<p>No winning results recorded yet.</p>
{% endif %}
</div><div class="card"><h3>✈️ Aviator Game</h3><p>Manage Aviator from this registered admin device.</p><a href="/aviator/admin" style="display:inline-block;padding:10px 14px;background:#7f1d1d;color:#fff;border-radius:6px;text-decoration:none;font-weight:bold">OPEN AVIATOR ADMIN</a></div></body></html>
"""



# ----------------------------------------------------
# ADDITIONAL BROWSER GAMES
# Browser-native ports of the uploaded standalone games.
# They use the website's existing player session, shared UGX balance and SQLite device sync.
# ----------------------------------------------------
EXTRA_GAME_LOCK = threading.RLock()
EXTRA_GAME_TEMPLATES = {
    "velocity": {
        "title": "VELOCITY CAR RACING", "emoji": "🏎️", "accent": "#60a5fa",
        "subtitle": "Choose the car you think will finish first.",
        "choices": [{"id":"TOYOTA","name":"TOYOTA GT","odds":3.40,"color":"#d72d37"}, {"id":"JUGER","name":"JUGER MUSCLE","odds":3.33,"color":"#2d69d7"}, {"id":"BENZ","name":"BENZ GT-R","odds":3.47,"color":"#e6c823"}, {"id":"BMW","name":"BMW M4 COUPE","odds":3.40,"color":"#888888"}],
        "min": 50, "max": 1000, "stake_label": "Stake (UGX 50–1,000)"
    },
    "chicken-clash": {
        "title": "CHICKEN CLASH", "emoji": "🐔", "accent": "#fb7185",
        "subtitle": "Pick the fighter that will win the arena battle.",
        "choices": [{"id":"RED ROOSTER","name":"🔴 RED ROOSTER","odds":2.80,"color":"#ef4444"}, {"id":"BLUE BRAWLER","name":"🔵 BLUE BRAWLER","odds":2.74,"color":"#3b82f6"}, {"id":"YELLOW JET","name":"🟡 YELLOW JET","odds":2.86,"color":"#eab308"}, {"id":"BLACK SHADOW","name":"⬛ BLACK SHADOW","odds":2.80,"color":"#71717a"}],
        "min": 50, "max": 50000, "stake_label": "Stake (UGX 50–50,000)"
    },
    "hot-7-fruit": {
        "title": "HOT 7 FRUIT", "emoji": "🍒", "accent": "#4ade80",
        "subtitle": "Spin the fruit reels. Matching lines can win.",
        "choices": [], "min": 50, "max": 50000, "stake_label": "Stake (minimum UGX 50)"
    },
    "fortune-slots": {
        "title": "FORTUNE SLOTS", "emoji": "🎰", "accent": "#fbbf24",
        "subtitle": "Spin three reels and match symbols across a line.",
        "choices": [], "min": 50, "max": 50000, "stake_label": "Stake (minimum UGX 50)"
    }
}
EXTRA_FRUITS = ["🍋", "🍊", "🍇", "🍉", "🍒", "🔔", "7️⃣", "🍑"]
EXTRA_SLOT_SYMBOLS = [{"label":"0x","mult":0,"emoji":"❌"},{"label":"0.5x","mult":0.5,"emoji":"🍋"},{"label":"1x","mult":1,"emoji":"🍊"},{"label":"2x","mult":2,"emoji":"💎"},{"label":"3x","mult":3,"emoji":"7️⃣"}]

def _extra_balance(dev_id):
    info = connected_devices.get(dev_id)
    return float(info.get("balance", 0)) if info else 0.0

def _extra_result(game, stake, selection=None):
    """Resolve a browser round using rules adapted from the original Pydroid games."""
    if game == "velocity":
        choices = EXTRA_GAME_TEMPLATES[game]["choices"]
        # One car gets a race advantage, then all four are ranked with jitter.
        favored = random.choice(choices)
        scores = {c["id"]: random.uniform(0.25, 1.0) + (1.5 if c["id"] == favored["id"] else 0.0)
                  for c in choices}
        order = sorted(choices, key=lambda c: scores[c["id"]], reverse=True)
        winner = order[0]
        picked = next((c for c in choices if c["id"] == selection), None)
        won = bool(picked and picked["id"] == winner["id"])
        payout = round(stake + stake * (picked["odds"] - 1) * 0.80, 2) if won else 0.0
        return {"won": won, "payout": payout, "headline": f"🏁 Winner: {winner['name']}",
                "detail": "Finish order: " + " → ".join(c["name"] for c in order) +
                          (f". Your {picked['name']} finished first." if won else f". Your pick did not win."),
                "display": winner["name"], "order": [c["id"] for c in order]}
    if game == "chicken-clash":
        choices = EXTRA_GAME_TEMPLATES[game]["choices"]
        # Weighted battle result approximates the original fighters' power/speed/stamina odds.
        weights = [random.uniform(0.4, 2.0) + random.uniform(0.15, 0.9) for _ in choices]
        winner = random.choices(choices, weights=weights, k=1)[0]
        picked = next((c for c in choices if c["id"] == selection), None)
        won = bool(picked and picked["id"] == winner["id"])
        payout = round(stake * picked["odds"], 2) if won else 0.0
        return {"won": won, "payout": payout, "headline": f"🏆 Winner: {winner['name']}",
                "detail": (f"Your fighter won! Payout UGX {payout:,.0f}." if won else f"Your fighter lost. Winner: {winner['name']}."),
                "display": winner["name"], "winner_id": winner["id"]}
    if game == "hot-7-fruit":
        # Follow the original Pydroid3 Hot 7 Fruit flow: choose the multiplier
        # first, spin a 3x3 grid, pay only if a row/column/diagonal matches, and
        # keep payouts within the available demo vault above its protected floor.
        symbols = ["🍋", "🍊", "🍇", "🍉", "🍒", "🔔", "7️⃣", "🍑"]
        roll = random.random()
        mult = 5 if roll < 0.05 else (3 if roll < 0.15 else (2 if roll < 0.35 else 1))
        available = max(0.0, float(house_vault) - PROTECTED_RESERVE)
        if float(house_vault) <= PROTECTED_RESERVE + 1500:
            grid = [[random.choice(symbols) for _ in range(3)] for _ in range(3)]
            payout = 0.0
            mult = 1
        elif random.random() < 0.25 and available > 0 and mult > 1:
            match = random.choice(["7️⃣", "🔔", "🍉", "🍇"])
            grid = [[match, random.choice(symbols), random.choice(symbols)],
                    [match, random.choice(symbols), random.choice(symbols)],
                    [match, random.choice(symbols), random.choice(symbols)]]
            payout = min(round(stake * mult * 0.80, 2), available)
        else:
            grid = [[random.choice(symbols) for _ in range(3)] for _ in range(3)]
            lines = [grid[0], grid[1], grid[2],
                     [grid[0][0], grid[1][1], grid[2][2]],
                     [grid[0][2], grid[1][1], grid[2][0]],
                     [grid[0][0], grid[1][0], grid[2][0]],
                     [grid[0][1], grid[1][1], grid[2][1]],
                     [grid[0][2], grid[1][2], grid[2][2]]]
            match = next((line[0] for line in lines if line[0] == line[1] == line[2]), None)
            payout = min(round(stake * mult, 2), available) if match else 0.0
        return {"won": payout > 0, "payout": payout,
                "headline": f"{'🎉 WIN' if payout else 'TRY AGAIN'}{f' — {mult}x' if payout else ''}",
                "detail": f"Matching line: {match}. Multiplier: {mult}x." if payout else "No payable matching line this spin.",
                "reels": grid, "multiplier": mult}
    # Fortune Slots: original rules use a 3x3 multiplier grid and the middle row wins.
    pool = [0.0, 0.0, 0.0, 0.5, 0.5, 1.0, 1.0, 2.0, 3.0, 5.0, 10.0]
    grid = [[random.choice(pool) for _ in range(3)] for _ in range(3)]
    middle = grid[1]
    matched = middle[0] == middle[1] == middle[2] and middle[0] > 0
    mult = middle[0] if matched else 0.0
    payout = round(stake * mult, 2) if matched else 0.0
    # Keep the existing reserve protected when paying from the demo House Vault.
    available = max(0.0, float(house_vault) - PROTECTED_RESERVE)
    payout = min(payout, available)
    return {"won": payout > 0, "payout": payout,
            "headline": f"{'✨ WIN' if payout else 'NO WIN'}{f' — {mult:g}x' if payout else ''}",
            "detail": f"Middle row matched at {mult:g}x." if matched else "The middle row did not match. Try again.",
            "reels": [[f"{x:g}x" if x else "❌" for x in row] for row in grid],
            "grid": grid, "multiplier": mult}

EXTRA_GAME_TEMPLATE = r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{g.title}}</title><style>
*{box-sizing:border-box}body{margin:0;background:#080d18;color:#f8fafc;font-family:Arial,sans-serif;padding:14px}main{max-width:620px;margin:auto}.card{background:#111c2d;border:1px solid #2c3b52;border-radius:14px;padding:15px;margin:10px 0}.top{display:flex;justify-content:space-between;align-items:center;gap:10px}.brand{font-weight:900;color:{{g.accent}}}.balance{font-weight:900;color:#4ade80}.nav{color:#cbd5e1;text-decoration:none;font-size:13px}.hero{text-align:center;padding:22px 10px;background:radial-gradient(circle at top,#263b5c,#101827 70%)}.hero .emoji{font-size:54px}.hero h1{font-size:24px;margin:8px 0;color:{{g.accent}}}.muted{color:#9fb0c5;font-size:13px}.choices{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;margin-top:12px}.pick{background:#1c2a40;color:#fff;border:1px solid #3c506d;border-radius:10px;padding:14px 8px;font-weight:bold;cursor:pointer;min-height:72px}.pick.selected{outline:3px solid {{g.accent}};background:#24364d}.pick small{display:block;color:{{g.accent}};margin-top:5px}.stake{width:100%;background:#080f1c;color:#fff;border:1px solid #425570;border-radius:9px;padding:13px;margin:12px 0;font-size:17px}.play{width:100%;border:0;border-radius:9px;padding:15px;background:{{g.accent}};color:#08111e;font-weight:900;font-size:16px;cursor:pointer}.play:disabled{opacity:.5}.result{font-weight:bold;font-size:18px;text-align:center;color:{{g.accent}}}.reels{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;text-align:center;margin:14px 0}.reel{font-size:40px;background:#080f1c;border:1px solid #41516b;border-radius:10px;padding:14px 4px;min-height:78px}.history{color:#cbd5e1;font-size:13px;line-height:1.6;white-space:pre-wrap}.race-track{display:grid;gap:8px;margin:12px 0}.lane{background:#080f1c;border:1px solid #354761;border-radius:8px;padding:8px;overflow:hidden}.car{display:block;font-size:25px;transition:transform 2.2s cubic-bezier(.15,.65,.2,1);white-space:nowrap}.arena{display:flex;justify-content:space-around;align-items:center;min-height:150px;background:radial-gradient(circle,#31445e,#080f1c);border-radius:12px;font-size:52px}.fighter{transition:transform .25s ease;display:inline-block}.fighter.hit{transform:translateX(14px) rotate(18deg)}.reel.spin{filter:blur(1px);transform:scaleY(.9)}
</style></head><body><main><div class="card top"><a class="nav" href="/">← GAME LOBBY</a><span class="brand">{{g.emoji}} {{g.title}}</span><span class="balance" id="balance">UGX {{'%.0f'|format(balance)}}</span></div><div class="card hero"><div class="emoji">{{g.emoji}}</div><h1>{{g.title}}</h1><div class="muted">{{g.subtitle}}</div></div>
{% if game in ['velocity','chicken-clash'] %}<div class="card"><b>Choose your winner</b><div class="choices" id="choices">{% for c in g.choices %}<button class="pick" data-id="{{c.id}}" onclick="pick('{{c.id}}',this)" style="border-top:4px solid {{c.color}}">{{c.name}}<small>{{'%.2f'|format(c.odds)}}x odds</small></button>{% endfor %}</div></div>{% if game=='velocity' %}<div class="card"><b>Race track</b><div class="race-track" id="raceTrack">{% for c in g.choices %}<div class="lane" data-car="{{c.id}}"><span class="car" style="color:{{c.color}}">🏎️ {{c.name}}</span></div>{% endfor %}</div><div class="muted" id="raceStatus">Waiting for race start</div></div>{% else %}<div class="card"><b>Battle arena</b><div class="arena" id="arena"><span class="fighter">🐓</span><span>⚔️</span><span class="fighter">🐓</span></div><div class="muted" id="battleStatus">Fighters are ready</div></div>{% endif %}{% else %}<div class="card"><div class="muted">{{'Match a line of fruit symbols to win' if game=='hot-7-fruit' else 'Match all three symbols on the middle row'}}</div><div class="reels" id="reels">{% for i in range(9) %}<div class="reel">❔</div>{% endfor %}</div></div>{% endif %}
<div class="card"><label for="stake">{{g.stake_label}}</label><input id="stake" class="stake" type="number" min="{{g.min}}" max="{{g.max}}" value="500"><button class="play" id="play" onclick="playGame()">{{'START RACE' if game=='velocity' else ('START BATTLE' if game=='chicken-clash' else ('SPIN FRUIT REELS' if game=='hot-7-fruit' else 'SPIN FORTUNE SLOTS'))}}</button><p class="muted" id="message">Choose your selection and play. The stake is deducted from your shared website balance.</p><div class="result" id="result"></div><div class="history" id="detail"></div></div></main>
<script>const GAME={{game|tojson}};let selected=null;function pick(id,el){selected=id;document.querySelectorAll('.pick').forEach(x=>x.classList.remove('selected'));el.classList.add('selected')}const wait=ms=>new Promise(r=>setTimeout(r,ms));async function animateRound(){if(GAME==='velocity'){document.getElementById('raceStatus').textContent='🏁 3… 2… 1… GO!';document.querySelectorAll('.car').forEach((c,i)=>{c.style.transform='translateX('+Math.round(100+Math.random()*240)+'px)'});await wait(2300)}else if(GAME==='chicken-clash'){let fs=[...document.querySelectorAll('.fighter')];document.getElementById('battleStatus').textContent='Battle in progress!';for(let i=0;i<5;i++){fs[0].style.transform='translateX(18px) rotate(12deg)';fs[1].style.transform='translateX(-18px) rotate(-12deg)';await wait(180);fs[0].style.transform='translateX(-8px)';fs[1].style.transform='translateX(8px)';await wait(180)}await wait(250)}else{const reels=[...document.querySelectorAll('.reel')];reels.forEach(x=>x.classList.add('spin'));let symbols=GAME==='hot-7-fruit'?['🍋','🍊','🍇','🍉','🍒','🔔','7️⃣','🍑']:['❌','0.5x','1x','2x','3x','5x','10x'];let timer=setInterval(()=>reels.forEach(x=>x.textContent=symbols[Math.floor(Math.random()*symbols.length)]),70);await wait(950);clearInterval(timer);reels.forEach(x=>x.classList.remove('spin'))}}async function playGame(){const btn=document.getElementById('play'),stake=Number(document.getElementById('stake').value);if(['velocity','chicken-clash'].includes(GAME)&&!selected){document.getElementById('message').textContent='Choose a car or fighter first.';return}btn.disabled=true;document.getElementById('message').textContent='Round starting…';document.getElementById('result').textContent='';try{await animateRound();const r=await fetch('/extra-games/play',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({game:GAME,stake,selection:selected})});const d=await r.json();document.getElementById('balance').textContent='UGX '+Math.floor(d.balance||0).toLocaleString();document.getElementById('message').textContent=d.message||'';if(d.success){document.getElementById('result').textContent=d.result.headline;document.getElementById('detail').textContent=d.result.detail+'\nStake: UGX '+stake.toLocaleString()+' | Payout: UGX '+Number(d.result.payout).toLocaleString();if(d.result.reels){let flat=d.result.reels.flat();document.getElementById('reels').innerHTML=flat.map(x=>'<div class="reel">'+x+'</div>').join('');if(GAME==='fortune-slots'&&d.result.grid){document.getElementById('reels').innerHTML=d.result.reels.flat().map(x=>'<div class="reel">'+x+'</div>').join('')} }if(GAME==='velocity'){document.getElementById('raceStatus').textContent=d.result.detail;let winner=d.result.order?.[0];document.querySelectorAll('.lane').forEach(l=>{let car=l.querySelector('.car');car.style.transform='translateX('+(l.dataset.car===winner?300:80)+'px)'})}if(GAME==='chicken-clash'){document.getElementById('battleStatus').textContent=d.result.headline;document.getElementById('arena').innerHTML='<span class="fighter">🐓</span><span>🏆</span><span class="fighter">🐓</span>'}}else{document.getElementById('result').textContent='';document.getElementById('detail').textContent=''}}catch(e){document.getElementById('message').textContent='Connection error. Please try again.'}finally{btn.disabled=false}}</script></body></html>'''

# ----------------------------------------------------
# Shared automatic rounds for Velocity and Chicken Clash.
# Timing and betting behavior follow the original Pydroid3 versions.
# ----------------------------------------------------
ROUND_GAME_LOCK = threading.RLock()
ROUND_GAME_STATE = {
    "velocity": {"status":"BETTING", "round_id":1, "phase_started":time.time(), "phase_duration":10.0,
                  "bets":{}, "winner":None, "order":[], "message":"PLACE YOUR BETS (10s)", "history":[], "player_results":{}},
    "chicken-clash": {"status":"BETTING", "round_id":1, "phase_started":time.time(), "phase_duration":15.0,
                      "bets":{}, "winner":None, "order":[], "message":"BETTING OPEN (15s)", "history":[], "player_results":{}}
}
ROUND_GAME_CONFIG = {
    "velocity": {"betting":10.0, "action":8.0, "result":4.0, "margin":0.20},
    "chicken-clash": {"betting":15.0, "action":6.0, "result":4.0, "margin":0.30}
}

def _choose_round_winner(game, round_id):
    rng = random.Random((int(round_id) * 982451653) + (401 if game == 'velocity' else 809))
    choices = EXTRA_GAME_TEMPLATES[game]['choices']
    if game == 'velocity':
        # The original game gives one randomly selected car a performance boost,
        # then ranks the entire field by the race simulation result.
        boosted = rng.choice(choices)
        scores = {c['id']: rng.uniform(0.0, 1.0) + (1.4 if c['id'] == boosted['id'] else 0.0)
                  for c in choices}
        order = sorted(choices, key=lambda c: scores[c['id']], reverse=True)
        return order[0]['id'], [c['id'] for c in order]
    # Original Chicken Clash chooses by fighter power, speed and stamina, with jitter.
    stats = {"RED ROOSTER":(85,70,80), "BLUE BRAWLER":(90,65,85),
             "YELLOW JET":(65,95,70), "BLACK SHADOW":(80,80,75)}
    weights = []
    for c in choices:
        power, speed, stamina = stats[c['id']]
        weights.append(max(10, (power + speed + stamina) * rng.uniform(0.4,2.0) + rng.randint(-60,60)))
    winner = rng.choices(choices, weights=weights, k=1)[0]['id']
    return winner, [winner] + [c['id'] for c in choices if c['id'] != winner]

def _settle_round_game(game, state):
    winner = state['winner']
    choices = {c['id']:c for c in EXTRA_GAME_TEMPLATES[game]['choices']}
    margin = ROUND_GAME_CONFIG[game]['margin']
    for dev_id, bets in list(state['bets'].items()):
        info = connected_devices.get(dev_id)
        if not info:
            continue
        summaries=[]; gross=0.0; total_stake=sum(float(b['stake']) for b in bets.values())
        for selection, bet in bets.items():
            stake=float(bet['stake']); odds=float(bet['odds'])
            if selection == winner:
                if game == 'velocity':
                    payout=stake + stake * (odds - 1.0) * (1.0 - margin)
                else:
                    payout=float(int(stake * odds))
                gross += payout
                summaries.append(f"Won on {choices[selection]['name']} — payout UGX {payout:,.0f}")
            else:
                summaries.append(f"Lost on {choices[selection]['name']} — UGX {stake:,.0f}")
        if gross:
            with lock:
                info['balance']=round(float(info['balance'])+gross,2)
                sync_device_to_db(dev_id)
        state['player_results'][dev_id] = {
            'won': gross > 0, 'payout': round(gross,2), 'stake':round(total_stake,2),
            'net':round(gross-total_stake,2), 'summary':' | '.join(summaries),
            'round_id':state['round_id'], 'winner':choices[winner]['name']
        }
    state['history'].insert(0, {"round":state['round_id'], "winner":choices[winner]['name']})
    del state['history'][10:]
    state['message'] = f"WINNER: {choices[winner]['name']}"

def _round_game_loop(game):
    state = ROUND_GAME_STATE[game]
    cfg = ROUND_GAME_CONFIG[game]
    while True:
        try:
            now=time.time()
            with ROUND_GAME_LOCK:
                elapsed=now-state['phase_started']
                if state['status']=='BETTING' and elapsed >= cfg['betting']:
                    state['winner'], state['order'] = _choose_round_winner(game,state['round_id'])
                    state['status']='RACING' if game=='velocity' else 'BATTLE'
                    state['phase_started']=now; state['phase_duration']=cfg['action']
                    state['message']='RACE STARTED — BETS LOCKED' if game=='velocity' else 'BATTLE IN PROGRESS — BETS LOCKED'
                elif state['status'] in ('RACING','BATTLE') and elapsed >= cfg['action']:
                    state['status']='RESULT'; state['phase_started']=now; state['phase_duration']=cfg['result']
                    _settle_round_game(game,state)
                elif state['status']=='RESULT' and elapsed >= cfg['result']:
                    state['round_id'] += 1
                    state['status']='BETTING'; state['phase_started']=now
                    state['phase_duration']=cfg['betting']; state['bets']={}
                    state['winner']=None; state['order']=[]
                    state['message']=f"PLACE YOUR BETS ({int(cfg['betting'])}s)" if game=='velocity' else f"BETTING OPEN ({int(cfg['betting'])}s)"
            time.sleep(0.15)
        except Exception as exc:
            app.logger.exception('Automatic %s round loop error: %s', game, exc)
            time.sleep(0.5)

if not app.config.get('ROUND_GAME_LOOPS_STARTED'):
    app.config['ROUND_GAME_LOOPS_STARTED']=True
    for _game in ('velocity','chicken-clash'):
        threading.Thread(target=_round_game_loop, args=(_game,), daemon=True, name=f'{_game}-round-loop').start()

ROUND_GAME_TEMPLATE = r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{g.title}}</title><style>
*{box-sizing:border-box}body{margin:0;background:#07152b;color:#fff;font-family:Arial;padding:12px}main{max-width:680px;margin:auto}.card{background:#102542;border:1px solid #27486e;border-radius:12px;padding:13px;margin:9px 0}.top{display:flex;justify-content:space-between;gap:8px;align-items:center}.brand{font-weight:900;color:{{g.accent}}}.bal{color:#86efac;font-weight:900}.nav{color:#b7c7dc;text-decoration:none;font-size:12px}.phase{text-align:center;font-size:19px;font-weight:900;color:{{g.accent}}}.timer{text-align:center;font-size:30px;font-weight:900}.muted{color:#b4c6dc;font-size:12px}.choices{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.pick{background:#09182e;color:white;border:1px solid #345579;border-radius:9px;padding:11px;cursor:pointer;text-align:center}.pick:disabled{opacity:.45}.pick b{display:block;margin-bottom:4px}.stake{width:100%;padding:12px;background:#07152b;color:white;border:1px solid #345579;border-radius:8px;font-size:16px}.lane{background:#07152b;border-radius:8px;padding:7px;margin:7px 0;overflow:hidden}.car{display:inline-block;font-size:25px;transition:transform .6s linear}.arena{display:flex;justify-content:space-around;align-items:center;font-size:45px;min-height:130px;background:radial-gradient(circle,#243e5f,#07152b);border-radius:9px}.fighter{display:inline-block;transition:transform .2s}.result{background:#082f24;color:#a7f3d0;padding:10px;border-radius:8px;white-space:pre-wrap}.loss{background:#3a1722;color:#ffc5ce}.small{font-size:12px;color:#9eb4ce}</style></head><body><main><div class="card top"><a class="nav" href="/">← GAME LOBBY</a><span class="brand">{{g.emoji}} {{g.title}}</span><span class="bal" id="balance">UGX {{'%.0f'|format(balance)}}</span></div><div class="card"><div class="phase" id="phase">BETTING OPEN</div><div class="timer" id="timer">--</div><div class="muted" id="message" style="text-align:center">Connecting to shared round…</div><div class="small" style="text-align:center;margin-top:5px">Round <span id="round">1</span> · All players share the same result</div></div>{% if game=='velocity' %}<div class="card"><b>🏁 Live race track</b><div id="track">{% for c in g.choices %}<div class="lane" data-id="{{c.id}}"><span class="car" style="color:{{c.color}}">🏎️ {{c.name}}</span></div>{% endfor %}</div></div>{% else %}<div class="card"><b>⚔️ Battle arena</b><div class="arena"><span class="fighter" id="fighter1">🐓</span><span>⚔️</span><span class="fighter" id="fighter2">🐓</span></div><div class="muted" id="battleText" style="text-align:center">Fighters are entering the arena.</div></div>{% endif %}<div class="card"><b>Place bets before the round starts</b><p class="muted">You can bet on more than one {{'car' if game=='velocity' else 'fighter'}} in the same betting window. Each selection can be bet once per round.</p><label>Stake (UGX)<input id="stake" class="stake" type="number" min="1" max="{{g.max}}" value="100"></label><div class="choices" style="margin-top:10px">{% for c in g.choices %}<button class="pick" id="pick-{{c.id}}" onclick="place('{{c.id}}')" style="border-top:4px solid {{c.color}}"><b>{{c.name}}</b><span>{{'%.2f'|format(c.odds)}}x odds</span><div class="small" id="bet-{{c.id}}">Not selected</div></button>{% endfor %}</div><div id="lastResult" class="result" style="display:none;margin-top:10px"></div></div><p class="small"><a class="nav" href="/">Return to lobby</a></p></main><script>
const GAME={{game|tojson}};const wait=ms=>new Promise(r=>setTimeout(r,ms));let lastRound=-1;let phaseSeen='';async function place(selection){let stake=Number(document.getElementById('stake').value||0);try{let r=await fetch('/round-game/bet',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({game:GAME,selection,stake})});let d=await r.json();document.getElementById('message').textContent=d.message||'';if(d.balance!==undefined)document.getElementById('balance').textContent='UGX '+Math.floor(d.balance).toLocaleString();sync()}catch(e){document.getElementById('message').textContent='Connection error. Try again.'}}
function draw(s){document.getElementById('balance').textContent='UGX '+Math.floor(s.balance||0).toLocaleString();document.getElementById('phase').textContent=s.status==='BETTING'?'BETTING OPEN':s.status==='RACING'?'🏁 RACE IN PROGRESS':s.status==='BATTLE'?'⚔️ BATTLE IN PROGRESS':'ROUND RESULT';document.getElementById('timer').textContent=Math.max(0,Math.ceil(s.seconds_left||0))+'s';document.getElementById('message').textContent=s.message||'';document.getElementById('round').textContent=s.round_id;document.querySelectorAll('.pick').forEach(b=>b.disabled=s.status!=='BETTING');document.querySelectorAll('[id^="bet-"]').forEach(e=>e.textContent='Not selected');(s.my_bets||[]).forEach(b=>{let el=document.getElementById('bet-'+b.selection);if(el)el.textContent='BET PLACED · UGX '+Number(b.stake).toLocaleString()});if(s.status==='RACING'&&GAME==='velocity'){let rank=s.order||[];document.querySelectorAll('.lane').forEach(l=>{let idx=rank.indexOf(l.dataset.id);l.querySelector('.car').style.transform='translateX('+Math.max(5,250-idx*55)+'px)'})}else if(s.status==='BETTING'&&GAME==='velocity'){document.querySelectorAll('.car').forEach(c=>c.style.transform='translateX(0px)')}if(GAME==='chicken-clash'){let f1=document.getElementById('fighter1'),f2=document.getElementById('fighter2');if(s.status==='BATTLE'){f1.style.transform='translateX(24px) rotate(14deg)';f2.style.transform='translateX(-24px) rotate(-14deg)';document.getElementById('battleText').textContent='The fighters are exchanging attacks!'}else{f1.style.transform='';f2.style.transform='';document.getElementById('battleText').textContent=s.status==='RESULT'?'Winner: '+(s.winner_name||'—'):'Fighters are ready'}}let res=document.getElementById('lastResult');if(s.last_result){res.style.display='block';res.className='result '+(s.last_result.won?'':'loss');res.textContent=(s.last_result.won?'YOU WON':'ROUND SETTLED')+' · '+s.last_result.summary+'\nStake: UGX '+Number(s.last_result.stake||0).toLocaleString()+' · Payout: UGX '+Number(s.last_result.payout||0).toLocaleString()+' · Net: UGX '+Number(s.last_result.net||0).toLocaleString()}if(lastRound!==s.round_id){lastRound=s.round_id}}
function sync(){fetch('/round-game/state?game='+encodeURIComponent(GAME)).then(r=>r.json()).then(draw).catch(()=>{})}setInterval(sync,500);sync();
</script></body></html>'''

@app.route('/round-game/state')
def round_game_state():
    game=request.args.get('game','')
    if game not in ROUND_GAME_STATE: return jsonify({'success':False,'message':'Unknown round game.'}),400
    dev_id=session.get('device_id'); info=connected_devices.get(dev_id,{'balance':0})
    with ROUND_GAME_LOCK:
        st=ROUND_GAME_STATE[game]
        elapsed=time.time()-st['phase_started']
        my_bets=[{'selection':sel,'stake':b['stake'],'odds':b['odds']} for sel,b in st['bets'].get(dev_id,{}).items()]
        winner_name=next((c['name'] for c in EXTRA_GAME_TEMPLATES[game]['choices'] if c['id']==st['winner']),None)
        return jsonify({'success':True,'game':game,'status':st['status'],'round_id':st['round_id'],
                        'seconds_left':max(0,st['phase_duration']-elapsed),'message':st['message'],
                        'winner_name':winner_name,'order':st['order'],'history':st['history'],
                        'my_bets':my_bets,'last_result':st['player_results'].get(dev_id),
                        'balance':float(info.get('balance',0))})

@app.route('/round-game/bet', methods=['POST'])
def round_game_bet():
    data=request.get_json(silent=True) or {}; game=data.get('game'); selection=data.get('selection')
    if game not in ROUND_GAME_STATE: return jsonify({'success':False,'message':'Unknown game.'}),400
    cfg=EXTRA_GAME_TEMPLATES[game]; dev_id=session.get('device_id'); info=connected_devices.get(dev_id)
    if not info: return jsonify({'success':False,'message':'Player wallet not found.'}),400
    try: stake=round(float(data.get('stake',0)),2)
    except (TypeError,ValueError): stake=0
    if selection not in [c['id'] for c in cfg['choices']]: return jsonify({'success':False,'message':'Choose a valid selection.'}),400
    if stake < 1 or stake > cfg['max']: return jsonify({'success':False,'message':f"Stake must be between UGX 1 and UGX {cfg['max']:,}."})
    with ROUND_GAME_LOCK:
        st=ROUND_GAME_STATE[game]
        if st['status']!='BETTING': return jsonify({'success':False,'message':'Betting is closed. Wait for the next round.','balance':info['balance']})
        player_bets=st['bets'].setdefault(dev_id,{})
        if selection in player_bets: return jsonify({'success':False,'message':'You already bet on this selection in this round.','balance':info['balance']})
        with lock:
            if float(info.get('balance',0)) < stake: return jsonify({'success':False,'message':'Insufficient wallet balance.','balance':info['balance']})
            choice=next(c for c in cfg['choices'] if c['id']==selection)
            info['balance']=round(float(info['balance'])-stake,2)
            sync_device_to_db(dev_id)
            player_bets[selection]={'stake':stake,'odds':choice['odds']}
        st['player_results'].pop(dev_id,None)
        return jsonify({'success':True,'message':f"Bet placed on {choice['name']} for UGX {stake:,.0f}.",'balance':info['balance']})

@app.route('/velocity')
def velocity_page():
    dev_id=session.get('device_id'); info=connected_devices.get(dev_id,{"balance":0})
    return render_template_string(ROUND_GAME_TEMPLATE, g=EXTRA_GAME_TEMPLATES['velocity'], game='velocity', balance=info.get('balance',0))

@app.route('/chicken-clash')
def chicken_clash_page():
    dev_id=session.get('device_id'); info=connected_devices.get(dev_id,{"balance":0})
    return render_template_string(ROUND_GAME_TEMPLATE, g=EXTRA_GAME_TEMPLATES['chicken-clash'], game='chicken-clash', balance=info.get('balance',0))

@app.route('/hot-7-fruit')
def hot_7_fruit_page():
    dev_id=session.get('device_id'); info=connected_devices.get(dev_id,{"balance":0})
    return render_template_string(EXTRA_GAME_TEMPLATE, g=EXTRA_GAME_TEMPLATES['hot-7-fruit'], game='hot-7-fruit', balance=info.get('balance',0))

@app.route('/fortune-slots')
def fortune_slots_page():
    dev_id=session.get('device_id'); info=connected_devices.get(dev_id,{"balance":0})
    return render_template_string(EXTRA_GAME_TEMPLATE, g=EXTRA_GAME_TEMPLATES['fortune-slots'], game='fortune-slots', balance=info.get('balance',0))

@app.route('/extra-games/play', methods=['POST'])
def extra_games_play():
    dev_id=session.get('device_id'); info=connected_devices.get(dev_id)
    if not info: return jsonify({"success":False,"message":"Player session not found."}),400
    data=request.get_json(silent=True) or {}; game=data.get('game'); selection=data.get('selection')
    if game not in EXTRA_GAME_TEMPLATES: return jsonify({"success":False,"message":"Unknown game."}),400
    if game in ('velocity','chicken-clash'): return jsonify({"success":False,"message":"These games use the automatic shared-round betting controls."}),400
    try: stake=round(float(data.get('stake',0)),2)
    except (TypeError,ValueError): stake=0
    config=EXTRA_GAME_TEMPLATES[game]
    if not (config['min'] <= stake <= config['max']): return jsonify({"success":False,"message":f"Stake must be between UGX {config['min']:,} and UGX {config['max']:,}."})
    if game in ('velocity','chicken-clash') and selection not in [c['id'] for c in config['choices']]:
        return jsonify({"success":False,"message":"Please choose a selection first."})
    with EXTRA_GAME_LOCK:
        with lock:
            if float(info.get('balance',0)) < stake:
                return jsonify({"success":False,"message":"Insufficient balance."})
            info['balance']=round(float(info['balance'])-stake,2)
            sync_device_to_db(dev_id)
        result=_extra_result(game,stake,selection)
        payout=max(0.0,round(float(result.get('payout',0)),2))
        if payout:
            with lock:
                info['balance']=round(float(info.get('balance',0))+payout,2)
                sync_device_to_db(dev_id)
        try:
            # Keep the portal's shared demo accounting aware of these game rounds.
            global house_vault, game_profit
            net = round(stake - payout, 2)
            house_vault = round(max(PROTECTED_RESERVE, house_vault + net), 2)
            game_profit = round(game_profit + max(0.0, net), 2)
            save_system_metric('house_vault', house_vault)
            save_system_metric('game_profit', game_profit)
            save_system_metric(f'extra_{game}_last_payout', payout)
            save_system_metric(f'extra_{game}_last_stake', stake)
        except Exception as exc:
            print(f'[EXTRA GAME ACCOUNTING] {type(exc).__name__}: {exc}', flush=True)
    return jsonify({"success":True,"message":"Round complete.","balance":info['balance'],"result":result})


if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5961, debug=False)