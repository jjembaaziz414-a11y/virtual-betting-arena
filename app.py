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

# Robustly target Render's persistent disk at /var/data so accounts & sessions survive redeploys/restarts
DATA_DIR = os.environ.get("DATA_DIR") or (
    "/var/data" if (os.environ.get("RENDER") or os.path.isdir("/var/data")) else BASE_DIR
)
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = BASE_DIR
    os.makedirs(DATA_DIR, exist_ok=True)

SECRET_KEY_FILE = os.path.join(DATA_DIR, "session_secret.key")

def load_or_create_secret_key():
    """Keep Flask session signatures stable across server restarts and redeploys."""
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
    PERMANENT_SESSION_LIFETIME=31536000  # keep login session for one full year
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
MATCH_DURATION = 93.0
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
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS system_state (
                key TEXT PRIMARY KEY,
                val_num REAL,
                val_str TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS devices (
                dev_id TEXT PRIMARY KEY,
                number INTEGER,
                color TEXT,
                balance REAL
            )
        ''')
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
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settled_rounds (
                round_idx INTEGER PRIMARY KEY
            )
        ''')
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
        metrics = {row['key']: (row['val_num'], row['val_str']) for row in cursor.execute("SELECT * FROM system_state")}
        
        house_vault = metrics.get('house_vault', (HOUSE_VAULT_INITIAL, None))[0]
        game_profit = metrics.get('game_profit', (0.0, None))[0]
        cycle_return_pool = metrics.get('cycle_return_pool', (0.0, None))[0]
        total_player_deposits = metrics.get('total_player_deposits', (0.0, None))[0]
        master_admin_device_id = metrics.get('master_admin_device_id', (0.0, None))[1]

        for row in cursor.execute("SELECT * FROM devices ORDER BY number ASC"):
            connected_devices[row['dev_id']] = {
                "number": row['number'],
                "color": row['color'],
                "balance": float(row['balance'])
            }
            if row['dev_id'] not in device_order:
                device_order.append(row['dev_id'])

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

        for row in cursor.execute("SELECT round_idx FROM settled_rounds"):
            settled_rounds.add(row['round_idx'])

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
            record["last_error"] = f"Insufficient return-pool funds."
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
        retry_pending_payout(record["id"])

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
    session.permanent = True
    path = request.path
    if path in ('/login', '/register', '/logout', '/health', '/debug/users'):
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
            return redirect(url_for('login_page'))
        return jsonify({'success': False, 'message': 'Please log in to use your shared player account.'}), 401
    if not session.get('account_id'):
        session.pop('device_id', None)

@app.route('/debug/users')
def debug_users():
    """Diagnostic route to check registered user accounts in the persistent database."""
    try:
        with get_db() as conn:
            rows = conn.execute('SELECT id, username, wallet_dev_id, created_at FROM player_accounts').fetchall()
            users = [dict(row) for row in rows]
        return jsonify({
            "success": True,
            "database_file": DB_FILE,
            "total_accounts": len(users),
            "accounts": users
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

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
        away_ht = shared_ht

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
    if bets_for_round:
        ft_keys = list(projected_payouts.keys())
        ht_keys = list(ht_projected_payouts.keys())
        combinations = []
        for ft_key in ft_keys:
            for ht_key in ht_keys:
                combined_payout = round(projected_payouts[ft_key] + ht_projected_payouts[ht_key], 2)
                combinations.append((ft_key, ht_key, combined_payout))

        affordable_combinations = [combo for combo in combinations if combo[2] <= cycle_return_pool]
        if affordable_combinations:
            weights = [1.0 / max(0.01, odds[ft_key] * ht_odds[ht_key]) for ft_key, ht_key, _ in affordable_combinations]
            selected = rng.choices(affordable_combinations, weights=weights, k=1)[0]
            ft_outcome, ht_outcome, selected_payout = selected
            if selected_payout > 0:
                return_pool_selected = True
        else:
            lowest_payout = min(combo[2] for combo in combinations)
            lowest_candidates = [combo for combo in combinations if combo[2] == lowest_payout]
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
                "ft_outcome": ft_outcome, "ht_outcome": ht_outcome,
                "final_home_goals": home_ft, "final_away_goals": away_ft,
                "final_home_ht_goals": home_ht, "final_away_ht_goals": away_ht,
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
        "round_idx": round_idx, "phase": phase,
        "time_left": round(time_left, 1), "match_elapsed": round(match_elapsed, 2),
        "match_finished": bool(match_finished),
        "half_time_break": bool(phase == "live" and 45.0 <= match_elapsed < 45.0 + HALF_TIME_BREAK_DURATION),
        "home": teams[0], "away": teams[1], "odds": odds, "ht_odds": ht_odds,
        "match_events": match_events, "target_ft_outcome": ft_outcome, "target_ht_outcome": ht_outcome,
        "final_home_goals": home_ft, "final_away_goals": away_ft,
        "demo_cycle_mode": cycle_mode, "demo_return_pool": cycle_return_pool,
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
                queue_pending_payout(dev_id, payout, ref, round_idx, "Waiting for return pool funds.", funding_source="return_pool")
            settled_rounds.add(round_idx)
            with get_db() as conn:
                conn.execute("INSERT OR IGNORE INTO settled_rounds (round_idx) VALUES (?)", (round_idx,))
                conn.commit()
            return

        for bet, payout in winning_bets:
            dev_id = bet['dev_id']
            connected_devices[dev_id]["balance"] = round(connected_devices[dev_id]["balance"] + payout, 2)
            sync_device_to_db(dev_id)

            cycle_return_pool = round(cycle_return_pool - payout, 2)
            save_system_metric('cycle_return_pool', cycle_return_pool)

            ref = f"win:round:{round_idx}:bet:{bet.get('id', uuid.uuid4().hex[:8])}"
            if not central_payout(dev_id, payout, ref):
                payout_id = queue_pending_payout(dev_id, payout, ref, round_idx, "Central payout delivery failed.", funding_source="return_pool")
                pending_payouts[payout_id]["local_credited"] = True
                sync_pending_payout_to_db(payout_id)

        settled_rounds.add(round_idx)
        with get_db() as conn:
            conn.execute("INSERT OR IGNORE INTO settled_rounds (round_idx) VALUES (?)", (round_idx,))
            conn.commit()

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
*{box-sizing:border-box}body{margin:0;background:#07152b;color:#f8fafc;font-family:Arial,sans-serif;min-height:100vh;display:grid;place-items:center;padding:18px}.auth{width:100%;max-width:430px;background:#102443;border:1px solid #25476f;border-radius:18px;padding:24px;box-shadow:0 20px 60px #0007}.brand{font-weight:900;color:#29d17d;letter-spacing:.5px;font-size:23px}.sub{color:#b7c7dc;font-size:13px;line-height:1.5}.field{width:100%;padding:13px;border-radius:9px;border:1px solid #345579;background:#08172d;color:#fff;margin:7px 0 13px;font-size:16px}.submit{width:100%;border:0;background:#25c76f;color:#062014;font-weight:900;padding:14px;border-radius:9px;cursor:pointer;font-size:16px}.link{color:#7de8ae}.error{background:#4b1d2b;color:#ffc4cf;padding:10px;border-radius:8px;margin:12px 0}.note{font-size:12px;color:#9eb1ca;margin-top:15px}</style></head><body><main class="auth"><div class="brand">⚡ VIRTUAL BETTING ARENA</div><h2>{{title}}</h2><p class="sub">One player account and one shared wallet across all games.</p>{% if error %}<div class="error">{{error}}</div>{% endif %}<form method="post"><label>Username</label><input class="field" name="username" required minlength="3" maxlength="30" autocomplete="username" placeholder="Enter username"><label>Password</label><input class="field" name="password" type="password" required minlength="6" autocomplete="{{'new-password' if mode=='register' else 'current-password'}}" placeholder="Enter password">{% if mode=='register' %}<label>Confirm password</label><input class="field" name="confirm" type="password" required minlength="6" autocomplete="new-password" placeholder="Repeat password">{% endif %}<button class="submit" type="submit">{{'CREATE ACCOUNT' if mode=='register' else 'LOG IN'}}</button></form><p>{% if mode=='register' %}Already registered? <a class="link" href="/login">Log in</a>{% else %}New player? <a class="link" href="/register">Create account</a>{% endif %}</p><div class="note">Accounts and balances are permanently stored. Closing your browser will keep you logged in when you return.</div></main></body></html>'''

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
                error = 'That username is already taken. Please log in instead.'
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
    return redirect(url_for('portal', deposit_message='Deposit request submitted.'))

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
                    conn.commit()
                elif action == 'reject':
                    conn.execute("UPDATE deposit_requests SET status='rejected', reviewed_at=?, reviewed_by=? WHERE id=?",
                                 (time.time(), session.get('username','admin'), req_id))
                    conn.commit()
        return redirect(url_for('admin_deposits'))
    with get_db() as conn:
        rows = conn.execute('SELECT d.*, a.username FROM deposit_requests d LEFT JOIN player_accounts a ON a.wallet_dev_id=d.wallet_dev_id ORDER BY d.id DESC LIMIT 100').fetchall()
    return render_template_string(r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Deposit Requests</title><style>body{background:#08162a;color:#fff;font-family:Arial;padding:16px}.wrap{max-width:900px;margin:auto}.item{background:#132947;border:1px solid #2d4a6b;border-radius:10px;padding:14px;margin:10px 0}.muted{color:#b3c4da}button{padding:10px;border:0;border-radius:6px;font-weight:bold;margin-right:5px}.approve{background:#22c55e}.reject{background:#ef4444;color:white}a{color:#86efac}</style></head><body><div class="wrap"><a href="/admin">← Admin panel</a><h1>Deposit requests</h1>{% for r in rows %}<div class="item"><b>{{r.username or 'Unknown account'}}</b> — UGX {{'%.0f'|format(r.amount)}}<p class="muted">Method: {{r.method}} · Reference: {{r.payment_reference}}<br>Status: {{r.status}}</p>{% if r.status=='pending' %}<form method="post"><input type="hidden" name="request_id" value="{{r.id}}"><button class="approve" name="action" value="approve">APPROVE</button><button class="reject" name="action" value="reject">REJECT</button></form>{% endif %}</div>{% else %}<p>No deposit requests yet.</p>{% endfor %}</div></body></html>''', rows=rows)

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
                return redirect(url_for('portal', error="Admin panel locked to another account!"))
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

    last_round = data["round_idx"] - 1
    last_result = round_result_cache.get(last_round)
    last_player_bets = [b for b in round_bets_ledger.get(last_round, []) if b.get("dev_id") == dev_id]
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
                "market": market, "selection_type": b.get("selection_type"),
                "stake": stake, "odds": odds_value, "won": won, "payout": payout
            })

        last_match_rng = random.Random(9999 + last_round)
        last_teams = last_match_rng.choice(TEAMS_POOL)
        last_player_summary = {
            "round_idx": last_round, "home": last_teams[0], "away": last_teams[1],
            "final_home_goals": last_result["final_home_goals"], "final_away_goals": last_result["final_away_goals"],
            "ft_outcome": ft_outcome, "ht_outcome": ht_outcome,
            "total_stake": round(total_stake, 2), "total_payout": round(total_payout, 2),
            "net": round(total_payout - total_stake, 2), "wins": wins, "losses": losses, "bets": player_results
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
        if round_data['phase'] != 'betting': return jsonify({"success": False, "message": "Betting window is closed!"})

        round_idx = round_data['round_idx']
        odds_source = round_data["ht_odds"] if market == "ht_result" else round_data["odds"]
        true_odds = round(float(odds_source.get(sel_type, 0)), 2)
        if true_odds <= 0: return jsonify({"success": False, "message": "Invalid odds."})

        effective_stake = round(stake, 2)
        commission = round(effective_stake * HOUSE_COMMISSION_RATE, 2)
        pool_portion = round(effective_stake - commission, 2)

        game_profit = round(game_profit + commission, 2)
        cycle_return_pool = round(cycle_return_pool + pool_portion, 2)

        save_system_metric('game_profit', game_profit)
        save_system_metric('cycle_return_pool', cycle_return_pool)

        connected_devices[dev_id]["balance"] = round(connected_devices[dev_id]["balance"] - effective_stake, 2)
        sync_device_to_db(dev_id)

        bet_id = uuid.uuid4().hex[:8]
        round_bets_ledger.setdefault(round_idx, []).append({
            "id": bet_id, "dev_id": dev_id, "stake": stake, "effective_stake": effective_stake,
            "market": market, "selection_type": sel_type, "odds": true_odds
        })

        with get_db() as conn:
            conn.execute('''
                INSERT INTO bets (id, dev_id, round_idx, stake, effective_stake, market, selection_type, odds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (bet_id, dev_id, round_idx, stake, effective_stake, market, sel_type, true_odds))
            conn.commit()

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
        recent_round_ids = [r[0] for r in conn.execute("SELECT DISTINCT round_idx FROM winning_history ORDER BY round_idx DESC LIMIT 3").fetchall()]
        recent_wins = []
        if recent_round_ids:
            placeholders = ",".join("?" for _ in recent_round_ids)
            recent_wins = [dict(row) for row in conn.execute(f"SELECT * FROM winning_history WHERE round_idx IN ({placeholders}) ORDER BY round_idx DESC, created_at DESC", recent_round_ids).fetchall()]
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
        elif action == 'remove':
            if connected_devices[dev_id]["balance"] < amount: return redirect(url_for('admin', error="Balance cannot drop below zero."))
            connected_devices[dev_id]["balance"] = round(connected_devices[dev_id]["balance"] - amount, 2)
            total_player_deposits = round(total_player_deposits - amount, 2)
            sync_device_to_db(dev_id)
            save_system_metric('total_player_deposits', total_player_deposits)
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
        elif action == 'remove':
            if house_vault - amount < PROTECTED_RESERVE:
                return redirect(url_for('admin', error=f"Cannot drop below reserve."))
            house_vault = round(house_vault - amount, 2)
            save_system_metric('house_vault', house_vault)
    return redirect(url_for('admin'))

# ----------------------------------------------------
# AVIATOR & OTHER GAMES (unchanged routing)
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
            for _ap in aviator_players.values():
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
    global aviator_house_balance, aviator_net_profit
    while True:
        try:
            with aviator_lock:
                aviator_game.update(status="WAITING", multiplier=1.0, message="NEXT ROUND STARTING...")
                for ap in list(aviator_players.values()):
                    for key, default in (("bet_active_1", False), ("bet_active_2", False),
                                         ("cashed_out_1", False), ("cashed_out_2", False),
                                         ("charged_1", False), ("charged_2", False),
                                         ("stake_1", 500), ("stake_2", 500)):
                        ap.setdefault(key, default)
                    if not ap.get("bet_active_1"): ap["cashed_out_1"] = ap["charged_1"] = False
                    if not ap.get("bet_active_2"): ap["cashed_out_2"] = ap["charged_2"] = False
            time.sleep(3.0)

            with aviator_lock:
                r = random.random()
                if r < .40: cp = round(random.uniform(1.00, 1.20), 2)
                elif r < .75: cp = round(random.uniform(1.21, 2.50), 2)
                elif r < .95: cp = round(random.uniform(2.51, 10.00), 2)
                else: cp = round(random.uniform(10.01, 50.00), 2)
                aviator_game.update(status="RUNNING", multiplier=1.0, crash_point=cp, message="PLANE TAKING OFF!")
                for dev_id, ap in list(aviator_players.items()):
                    info = connected_devices.get(dev_id)
                    if not info: continue
                    with lock:
                        for panel in (1, 2):
                            if ap.get(f"bet_active_{panel}") and not ap.get(f"charged_{panel}"):
                                stake = float(ap.get(f"stake_{panel}", 0))
                                if 0 < stake <= float(info.get("balance", 0)):
                                    info["balance"] = round(float(info["balance"]) - stake, 2)
                                    aviator_house_balance = round(aviator_house_balance + stake, 2)
                                    ap[f"charged_{panel}"] = True
                                    sync_device_to_db(dev_id)
                save_aviator_state()

            current_mult = 1.0
            while current_mult < cp:
                time.sleep(0.08)
                current_mult = round(current_mult + max(0.01, current_mult * 0.03), 2)
                if current_mult > cp: current_mult = cp
                with aviator_lock:
                    aviator_game["multiplier"] = current_mult
                    aviator_game["message"] = f"FLYING — {current_mult:.2f}x"

            with aviator_lock:
                aviator_game.update(status="CRASHED", multiplier=cp, message=f"FLEW AWAY @ {cp:.2f}x!")
                history = aviator_game.setdefault("history", [])
                history.insert(0, cp)
                del history[10:]
                for ap in list(aviator_players.values()):
                    for panel in (1, 2):
                        if ap.get(f"bet_active_{panel}") and not ap.get(f"cashed_out_{panel}"):
                            ap["last_result"] = f"Crashed @ {cp:.2f}x"
                        ap[f"bet_active_{panel}"] = ap[f"cashed_out_{panel}"] = ap[f"charged_{panel}"] = False
                save_aviator_state()
            time.sleep(3.0)
        except Exception:
            time.sleep(1.0)

if not app.config.get("AVIATOR_LOOP_STARTED"):
    app.config["AVIATOR_LOOP_STARTED"] = True
    threading.Thread(target=aviator_loop, daemon=True, name="aviator-loop").start()

AVIATOR_TEMPLATE = r'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Aviator</title>
<style>body{margin:0;background:#0f141c;color:#fff;font-family:Arial;padding:12px}main{max-width:500px;margin:auto}.top,.card{background:#182232;border:1px solid #27354f;border-radius:10px;padding:12px;margin-bottom:10px}.top{display:flex;justify-content:space-between;align-items:center}.brand{color:#ef4444;font-weight:bold}.bal{color:#4ade80;font-weight:bold}.flight{height:190px;background:#090d14;border:2px solid #27354f;border-radius:12px;display:flex;flex-direction:column;align-items:center;justify-content:center;margin-bottom:10px}.mult{font-size:40px;font-weight:bold}.panel{background:#182232;border:1px solid #27354f;padding:12px;border-radius:10px;margin-bottom:10px}.line{display:flex;justify-content:space-between;align-items:center}.stake{width:120px;padding:9px;background:#090d14;color:#facc15;border:1px solid #27354f;border-radius:6px}.action{width:100%;padding:12px;border:0;border-radius:7px;background:#22c55e;color:#fff;font-weight:bold}.action.cash{background:#eab308;color:#111}.nav{display:flex;gap:8px}.nav a{flex:1;text-align:center;padding:10px;background:#27354f;border-radius:7px;color:#fff;text-decoration:none}</style></head><body><main>
<div class="top"><span class="brand">✈ AVIATOR</span><span class="bal" id="bal">UGX {{ '%.0f'|format(balance) }}</span></div>
<div class="nav"><a href="/">Lobby</a><a href="/arena">Football</a></div>
<div class="flight"><div class="mult" id="mult">1.00x</div><div id="msg" style="color:#94a3b8;font-size:12px">Waiting</div></div>
<div id="result" style="text-align:center;margin-bottom:8px;font-size:13px;color:#facc15"></div>
{% for n in [1,2] %}<div class="panel"><div class="line"><b>Bet {{n}}</b><input class="stake" id="stake{{n}}" type="number" value="500"></div><button class="action" id="btn{{n}}" onclick="act({{n}})">BET</button></div>{% endfor %}
</main><script>
function act(panel){const b=document.getElementById('btn'+panel);const action=b.dataset.action==='cashout'?'cashout':'bet';const amount=Number(document.getElementById('stake'+panel).value||500);fetch('/aviator/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,panel,amount})}).then(r=>r.json()).then(draw).catch(()=>{});}
function draw(s){document.getElementById('bal').textContent='UGX '+Math.floor(s.balance||0).toLocaleString();document.getElementById('mult').textContent=Number(s.multiplier||1).toFixed(2)+'x';document.getElementById('msg').textContent=s.message;document.getElementById('result').textContent=s.last_result||'';[1,2].forEach(n=>{let b=document.getElementById('btn'+n);if(s.status==='RUNNING'&&s['bet_active_'+n]&&!s['cashed_out_'+n]){b.textContent='COLLECT';b.className='action cash';b.dataset.action='cashout';}else{b.textContent='BET';b.className='action';b.dataset.action='bet';}})}
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
    if not info: return jsonify({"error": "Not found"}), 400
    data = request.get_json(silent=True) or {}
    action = data.get('action')
    try: panel = int(data.get('panel', 1)); amount = float(data.get('amount', 500))
    except: return jsonify({"error": "Invalid"}), 400
    with aviator_lock:
        ap = aviator_player(dev_id)
        if action == 'bet':
            if aviator_game['status'] != 'WAITING': ap['last_result'] = 'Round started. Wait next.'
            elif amount <= 0 or amount > info['balance']: ap['last_result'] = 'Insufficient balance.'
            else:
                ap[f'stake_{panel}'] = amount; ap[f'bet_active_{panel}'] = True; ap[f'cashed_out_{panel}'] = False
                ap['last_result'] = f'Bet #{panel} queued.'
        elif action == 'cashout':
            if aviator_game['status'] == 'RUNNING' and ap[f'bet_active_{panel}'] and not ap[f'cashed_out_{panel}']:
                stake = float(ap[f'stake_{panel}'])
                payout = round(stake * aviator_game['multiplier'], 2)
                with lock:
                    info['balance'] = round(info['balance'] + payout, 2)
                    sync_device_to_db(dev_id)
                ap[f'cashed_out_{panel}'] = True
                ap['last_result'] = f'Collected {payout:,.0f} UGX'
        return jsonify({**aviator_game, **ap, "balance": info.get("balance", 0)})

PORTAL_TEMPLATE = r"""
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Virtual Betting Arena</title><style>
*{box-sizing:border-box}body{margin:0;background:#07152b;color:#f7fafc;font-family:Arial,sans-serif}.top{background:#0c203c;border-bottom:2px solid #1b8f59;padding:12px 16px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:5}.logo{font-weight:900;color:#35d780;font-size:20px}.bal{background:#123c31;color:#9fffc5;padding:9px 12px;border-radius:9px;font-weight:900}.wrap{max-width:1000px;margin:auto;padding:14px}.welcome{background:linear-gradient(115deg,#12355b,#10452f);padding:18px;border-radius:14px;margin-bottom:14px}.welcome h1{margin:0 0 6px;font-size:22px}.muted{color:#b5c7dc;font-size:13px}.games{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.game{display:flex;gap:12px;align-items:center;text-decoration:none;color:#fff;background:#102542;border:1px solid #27486e;border-radius:12px;padding:14px;min-height:90px}.game:hover{border-color:#37d783}.emoji{font-size:32px;width:45px;text-align:center}.tag{display:inline-block;color:#8df0b4;font-size:11px;margin-top:4px}.wallet{background:#102542;border:1px solid #27486e;border-radius:12px;padding:16px;margin-top:14px}.formgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.field{width:100%;padding:12px;background:#07172e;color:white;border:1px solid #365779;border-radius:8px;margin-top:5px}.submit{background:#2bd178;color:#062014;font-weight:900;border:0;border-radius:8px;padding:12px;cursor:pointer}.links{display:flex;gap:15px;margin-top:14px}.links a{color:#a7f3d0}.small{font-size:12px;color:#9fb4ce}@media(max-width:520px){.games{grid-template-columns:1fr}}
</style></head><body><header class="top"><div class="logo">⚡ VIRTUAL BETTING ARENA</div><div class="bal">UGX {{'%.0f'|format(balance)}}</div></header><main class="wrap"><section class="welcome"><h1>Welcome, {{account_name}}</h1><div class="muted">One account · One wallet · Play any game and keep the same balance everywhere.</div></section><div class="games"><a class="game" href="/arena"><span class="emoji">⚽</span><span><b>Virtual Football</b><span class="tag">PLAY NOW →</span></span></a><a class="game" href="/aviator"><span class="emoji">✈️</span><span><b>Aviator</b><span class="tag">PLAY NOW →</span></span></a></div><section class="wallet"><div class="section-title" style="margin-top:0">MY WALLET</div><form method="post" action="/deposit/request"><div class="formgrid"><label>Amount (UGX)<input class="field" type="number" name="amount" min="1000" max="10000000" required></label><label>Method<input class="field" name="method" required></label><label style="grid-column:1/-1">Reference<input class="field" name="reference" required></label></div><button class="submit" style="margin-top:12px;width:100%">SUBMIT DEPOSIT</button></form></section><div class="links"><a href="/logout">Log out</a><a href="/admin">Admin panel</a></div></main></body></html>
"""

MAIN_TEMPLATE = """
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Virtual Football Arena</title><style>
body{font-family:Arial;background:#07111c;color:#fff;margin:0;padding-bottom:120px}
header{background:#101d2c;padding:11px 15px;display:flex;justify-content:space-between;border-bottom:2px solid {{color}};align-items:center;position:sticky;top:0;z-index:10}
.logo{color:{{color}};font-weight:bold}.balance{background:#1c2b3c;padding:7px 13px;border-radius:16px;color:#49d7ff;font-weight:800}
.container{padding:10px;max-width:680px;margin:auto}.card{background:#0e1b29;border:1px solid #25415d;border-radius:10px;padding:12px;margin-bottom:10px}
.title{font-size:.9rem;font-weight:bold;color:#dcecff;margin:10px 0 7px;border-left:3px solid {{color}};padding-left:7px}
.grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}.odd{background:#18293b;color:#d7e5f3;border:1px solid #34516e;border-radius:6px;padding:9px;font-size:.8rem;cursor:pointer}
.odd.selected{background:{{color}};color:#001018;font-weight:bold}
.input{width:100%;box-sizing:border-box;padding:10px;background:#142333;color:#fff;border:1px solid #34516e;border-radius:5px;margin-top:6px}
.place{width:100%;padding:12px;margin-top:8px;background:{{color}};border:0;border-radius:5px;font-weight:bold;color:#001018;cursor:pointer}
.bet{position:fixed;bottom:0;left:0;width:100%;box-sizing:border-box;background:#0d1825;border-top:2px solid {{color}};padding:11px;z-index:20}
</style></head><body>
<header><a class="logo" href="/">⚡ Lobby</a><div class="balance" id="bal">UGX {{ "%.2f"|format(balance) }}</div></header>
<div class="container">
<div class="card" id="timer">Loading...</div>
<div class="card"><div id="match" style="font-weight:bold;font-size:1.05rem;margin-bottom:10px;color:#58cfff">Loading Match...</div>
  <div class="title">FULL TIME RESULT (1X2)</div>
  <div class="grid">
    <button class="odd market-option" id="ft_1" onclick="pickMarket('ft_result','1')">1<br><span id="o1">-</span></button>
    <button class="odd market-option" id="ft_X" onclick="pickMarket('ft_result','X')">X<br><span id="ox">-</span></button>
    <button class="odd market-option" id="ft_2" onclick="pickMarket('ft_result','2')">2<br><span id="o2">-</span></button>
  </div>
</div>
</div>
<div class="bet">
  <input id="stake" class="input" type="number" min="100" max="50000" placeholder="Stake UGX">
  <button id="place" class="place" onclick="bet()">PLACE BET</button>
</div>
<script>
const id="{{device_id}}";
let sel=null, odds={}, round=-1;
setInterval(sync,1000);
function sync(){
 fetch('/get_state').then(r=>r.json()).then(d=>{
   document.getElementById('bal').innerText='UGX '+Number(d.balance||0).toFixed(2);
   if(d.round_idx!==round){round=d.round_idx;sel=null;}
   odds=d.odds||{};
   document.getElementById('match').innerText=(d.home||'Home')+' vs '+(d.away||'Away');
   document.getElementById('o1').innerText=odds['1']??'-';
   document.getElementById('ox').innerText=odds['X']??'-';
   document.getElementById('o2').innerText=odds['2']??'-';
   document.getElementById('timer').innerText=d.phase==='betting'?'⏱️ BETTING OPEN — '+Math.ceil(d.time_left)+'s':'🔴 LIVE MATCH';
 }).catch(()=>{});
}
function pickMarket(market,type){
 sel={market,type};document.querySelectorAll('.market-option').forEach(b=>b.classList.remove('selected'));
 document.getElementById((market==='ht_result'?'ht_':'ft_')+type).classList.add('selected');
}
async function bet(){
 const s=Number(document.getElementById('stake').value);
 if(!sel)return alert('Pick a market first.');
 let r=await(await fetch('/place_bet',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_id:id,stake:s,selection:sel})})).json();
 alert(r.message);
}
sync();
</script></body></html>
"""

ADMIN_TEMPLATE = """
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin</title><style>body{font-family:Arial;background:#121212;color:#fff;padding:15px}.card{background:#1e1e1e;padding:15px;border-radius:8px;margin-bottom:12px}a{color:#28a745}</style></head>
<body><div style="max-width:800px;margin:auto"><a href="/">← Portal</a>
<div class="card"><h2>Admin Panel</h2><p>Game Profit: UGX {{ "%.2f"|format(displayed_profit) }}</p><p>Return Pool: UGX {{ "%.2f"|format(cycle_return_pool) }}</p></div></div></body></html>
"""

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5961, debug=False)
