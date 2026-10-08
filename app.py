from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
import os
import random, uuid, time, threading, secrets, sqlite3

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
SECRET_KEY_FILE = os.path.join(BASE_DIR, "session_secret.key")

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
    SESSION_COOKIE_SECURE=os.environ.get('HTTPS_ENABLED', '0') == '1'
)

lock = threading.Lock()
DB_FILE = os.path.join(BASE_DIR, "game_state.db")

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
MATCH_DURATION = 40.0
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

        # Player accounts for the website game lobby
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS player_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                dev_id TEXT UNIQUE NOT NULL,
                created_at REAL NOT NULL
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
    global master_admin_device_id, connected_devices, device_order, pending_payouts, settled_rounds

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
                "balance": 0.00
            }
            device_order.append(dev_id)
            sync_device_to_db(dev_id)
    return dev_id

@app.before_request
def track_device():
    dev_id = session.get('device_id')
    if not dev_id:
        dev_id = ensure_device()
        session['device_id'] = dev_id
    else:
        ensure_device(dev_id)

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
    else:
        phase = "live"
        time_left = CYCLE_DURATION - phase_elapsed

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
            round_result_cache[round_idx] = {
                "ft_outcome": ft_outcome,
                "ht_outcome": ht_outcome,
                "final_home_goals": home_ft,
                "final_away_goals": away_ft,
                "final_home_ht_goals": home_ht,
                "final_away_ht_goals": away_ht,
                "return_pool_selected": return_pool_selected
            }

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

@app.route('/')
def portal():
    if not session.get('player_id'):
        return redirect(url_for('login'))
    return redirect(url_for('games'))

LOGIN_TEMPLATE = """
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>JJ Virtual Betting Arena - Login</title><style>
body{font-family:Arial;background:#0b0e14;color:#fff;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}.card{background:#161b22;border:1px solid #30363d;border-radius:14px;padding:28px;width:90%;max-width:390px;text-align:center}h1{color:#28a745;margin-top:0}.input{width:100%;box-sizing:border-box;padding:12px;background:#21262d;color:#fff;border:1px solid #30363d;border-radius:7px;margin:7px 0}.btn{width:100%;padding:12px;border:0;border-radius:7px;margin-top:8px;font-weight:bold;cursor:pointer}.primary{background:#28a745;color:#000}.secondary{background:#21262d;color:#fff}.err{color:#ff6b6b}.demo{color:#8b949e;font-size:.85rem;margin-top:14px}</style></head><body><div class="card"><h1>JJ VIRTUAL ARENA ⚡</h1><h2>Player Login</h2>{% if error %}<p class="err">{{error}}</p>{% endif %}<form method="POST"><input class="input" name="username" placeholder="Username" required><input class="input" type="password" name="password" placeholder="Password" required><button class="btn primary">LOGIN</button></form><a href="/register"><button class="btn secondary">CREATE ACCOUNT</button></a><div class="demo">New demo accounts start with UGX 10,000.</div></div></body></html>
"""
REGISTER_TEMPLATE = """
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Create Account</title><style>body{font-family:Arial;background:#0b0e14;color:#fff;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}.card{background:#161b22;border:1px solid #30363d;border-radius:14px;padding:28px;width:90%;max-width:390px;text-align:center}h1{color:#28a745}.input{width:100%;box-sizing:border-box;padding:12px;background:#21262d;color:#fff;border:1px solid #30363d;border-radius:7px;margin:7px 0}.btn{width:100%;padding:12px;border:0;border-radius:7px;margin-top:8px;font-weight:bold}.primary{background:#28a745;color:#000}.err{color:#ff6b6b}</style></head><body><div class="card"><h1>CREATE PLAYER ACCOUNT</h1>{% if error %}<p class="err">{{error}}</p>{% endif %}<form method="POST"><input class="input" name="username" placeholder="Username" minlength="3" maxlength="30" required><input class="input" type="password" name="password" placeholder="Password" minlength="4" required><input class="input" type="password" name="confirm" placeholder="Confirm password" required><button class="btn primary">CREATE ACCOUNT</button></form><p><a href="/login" style="color:#58a6ff">Back to Login</a></p></div></body></html>
"""
GAMES_TEMPLATE = """
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Game Lobby</title><style>body{font-family:Arial;background:#0b0e14;color:#fff;margin:0}.header{padding:15px;background:#161b22;border-bottom:2px solid #28a745;display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap}.balance{color:#28a745;font-weight:bold}.wrap{max-width:650px;margin:auto;padding:18px}.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:18px;margin-bottom:12px}.play{display:block;text-align:center;text-decoration:none;background:#28a745;color:#000;padding:12px;border-radius:7px;font-weight:bold;margin-top:12px}.disabled{background:#30363d;color:#8b949e}.small{color:#8b949e;font-size:.85rem}.toplinks a{color:#58a6ff;text-decoration:none;margin-right:12px}</style></head><body><div class="header"><b>JJ VIRTUAL ARENA</b><span>Hi, {{username}}</span><span class="balance">UGX {{"%.2f"|format(balance)}}</span></div><div class="wrap"><h2>SELECT A GAME</h2><div class="card"><h2>⚽ Virtual Football</h2><p>Live virtual football • Full Time + Half Time betting • 50 second betting window</p><a class="play" href="/game/football">PLAY FOOTBALL</a></div><div class="card"><h2>🎴 Matatu</h2><p class="small">Card game — coming soon</p><div class="play disabled">COMING SOON</div></div><div class="card"><h2>🏎️ Virtual Racing</h2><p class="small">Virtual racing — coming soon</p><div class="play disabled">COMING SOON</div></div><div class="toplinks"><a href="/admin-access">Admin</a><a href="/logout">Logout</a></div></div></body></html>
"""
@app.route('/login', methods=['GET','POST'])
def login():
    error=None
    if request.method=='POST':
        username=request.form.get('username','').strip().lower(); password=request.form.get('password','')
        with get_db() as conn: row=conn.execute('SELECT * FROM player_accounts WHERE username=?',(username,)).fetchone()
        if not row or not check_password_hash(row['password_hash'],password): error='Invalid username or password.'
        else:
            session['player_id']=row['id']; session['username']=row['username']; session['device_id']=row['dev_id']; session['is_admin']=False
            ensure_device(row['dev_id']); return redirect(url_for('games'))
    return render_template_string(LOGIN_TEMPLATE,error=error)
@app.route('/register', methods=['GET','POST'])
def register():
    error=None
    if request.method=='POST':
        username=request.form.get('username','').strip().lower(); password=request.form.get('password',''); confirm=request.form.get('confirm','')
        if len(username)<3: error='Username must be at least 3 characters.'
        elif password!=confirm: error='Passwords do not match.'
        elif len(password)<4: error='Password must be at least 4 characters.'
        else:
            dev_id=str(uuid.uuid4())
            try:
                with lock:
                    with get_db() as conn: conn.execute('INSERT INTO player_accounts(username,password_hash,dev_id,created_at) VALUES (?,?,?,?)',(username,generate_password_hash(password),dev_id,time.time()))
                    ensure_device(dev_id); connected_devices[dev_id]['balance']=10000.00; sync_device_to_db(dev_id)
                with get_db() as conn: pid=conn.execute('SELECT id FROM player_accounts WHERE username=?',(username,)).fetchone()[0]
                session['player_id']=pid; session['username']=username; session['device_id']=dev_id; session['is_admin']=False
                return redirect(url_for('games'))
            except sqlite3.IntegrityError: error='That username is already taken.'
    return render_template_string(REGISTER_TEMPLATE,error=error)
@app.route('/games')
def games():
    if not session.get('player_id'): return redirect(url_for('login'))
    info=connected_devices.get(session.get('device_id'),{"balance":0})
    return render_template_string(GAMES_TEMPLATE,username=session.get('username','Player'),balance=info.get('balance',0))
@app.route('/game/<slug>')
def game(slug):
    if not session.get('player_id'): return redirect(url_for('login'))
    return redirect(url_for('arena')) if slug=='football' else redirect(url_for('games'))
@app.route('/logout')
def logout():
    session.clear(); return redirect(url_for('login'))

@app.route('/admin-access')
def admin_access():
    if not session.get('player_id'):
        return redirect(url_for('login'))
    dev_id=session.get('device_id'); info=connected_devices.get(dev_id,{"number":1,"color":"#28a745"})
    return render_template_string(PORTAL_TEMPLATE, device_number=info["number"], color=info["color"])

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
                return redirect(url_for('admin_access', error="Admin panel locked to another device!"))
        session['is_admin'] = True
        return redirect(url_for('admin'))
    return redirect(url_for('admin_access', error="Invalid PIN"))

@app.route('/arena')
def arena():
    if not session.get('player_id'):
        return redirect(url_for('login'))
    dev_id = session.get('device_id')
    info = connected_devices.get(dev_id, {"number": 1, "color": "#28a745", "balance": 0})
    return render_template_string(MAIN_TEMPLATE, device_id=dev_id, device_number=info["number"], color=info["color"], balance=info["balance"])

@app.route('/get_state')
def get_state():
    if not session.get('player_id'):
        return jsonify({'success':False,'message':'Please log in.'}), 401
    dev_id = session.get('device_id')
    data = get_current_round_data()
    info = connected_devices.get(dev_id, {"balance": 0})
    return jsonify({
        **data,
        "balance": info["balance"],
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
        "risk_model_note": "Game Profit records 20% commission on bets. Payouts are paid purely from Return Pool.",
        "accounting_note": "Game profit cannot be negative."
    })

@app.route('/place_bet', methods=['POST'])
def place_bet():
    if not session.get('player_id'):
        return jsonify({'success':False,'message':'Please log in.'}), 401
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

PORTAL_TEMPLATE = """
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Arena Portal</title><style>
body{font-family:Arial;background:#121212;color:#fff;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}
.card{background:#1e1e1e;border:1px solid #333;border-radius:12px;padding:35px;width:90%;max-width:400px;text-align:center}
h1{color:#28a745}.btn{display:block;background:#2c2c2c;color:#fff;padding:14px;margin:12px 0;border-radius:8px;text-decoration:none;border:1px solid #444;cursor:pointer;font-size:1rem;font-weight:bold;width:100%;box-sizing:border-box}
.badge{display:inline-block;padding:5px 10px;border-radius:15px;color:#000;font-weight:bold;background:{{color}}}
input[type=password]{width:100%;box-sizing:border-box;padding:12px;background:#2c2c2c;color:#fff;border:1px solid #444;border-radius:8px;margin:12px 0;text-align:center;font-size:1.2rem;letter-spacing:3px}
.err{color:#dc3545;font-size:0.85rem;margin-top:5px}
</style></head><body><div class="card"><h1>ARENA PORTAL ⚡</h1>
<p><span class="badge">Device {{device_number}}</span></p>
{% if request.args.get('error') %}<p class="err">❌ {{ request.args.get('error') }}</p>{% endif %}
<form method="POST" action="/admin-auth">
  <input type="password" name="pin" placeholder="Enter Admin PIN" required maxlength="4">
  <button type="submit" class="btn" style="background:#28a745;color:#000;">🛠️ Open Admin Panel</button>
</form>
<a class="btn" href="/arena" style="background:#21262d;margin-top:15px;">⚽ Open Virtual Broadcasting Screen</a></div></body></html>
"""

MAIN_TEMPLATE = """
<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Virtual Betting Arena</title><style>
body{font-family:Arial;background:#0b0e14;color:#fff;margin:0;padding-bottom:130px}
header{background:#161b22;padding:10px 15px;display:flex;justify-content:space-between;border-bottom:2px solid {{color}};align-items:center}
.logo{color:{{color}};font-weight:bold;font-size:0.95rem}.balance{background:#21262d;padding:6px 12px;border-radius:15px;color:{{color}};font-size:0.9rem}
.container{padding:10px;max-width:600px;margin:auto}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px;margin-bottom:10px;text-align:center}
.title{font-size:0.95rem;font-weight:bold;color:#f0f6fc;margin-bottom:8px;text-align:left;border-left:3px solid {{color}};padding-left:6px}
.grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}
.odd{background:#21262d;color:#c9d1d9;border:1px solid #30363d;border-radius:5px;padding:8px;font-size:0.8rem;cursor:pointer}
.odd.selected{background:{{color}};color:#000;font-weight:bold;border-color:{{color}}}
.input{width:100%;box-sizing:border-box;padding:10px;background:#21262d;color:#fff;border:1px solid #30363d;border-radius:5px;margin-top:6px}
.place{width:100%;padding:12px;margin-top:8px;background:{{color}};border:0;border-radius:5px;font-weight:bold;color:#000;cursor:pointer}
.place:disabled{background:#30363d;color:#8b949e}
.bet{position:fixed;bottom:0;left:0;width:100%;box-sizing:border-box;background:#161b22;border-top:2px solid {{color}};padding:12px;z-index:20}
.live{display:none;background:#090d16;position:fixed;inset:0;z-index:30;padding:8px;overflow-y:auto}
.tv-header{background:#11161d;border:1px solid #30363d;border-radius:6px;padding:8px 12px;display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;}
.tv-teams{font-weight:bold;font-size:0.95rem;color:#f0f6fc}
.tv-status{font-size:0.75rem;color:#8b949e}
.tv-scorebox{background:#1f242c;border:1px solid #30363d;padding:4px 14px;border-radius:4px;font-size:1.4rem;font-weight:bold;color:#58a6ff;letter-spacing:2px}
.pitch{height:380px;background:radial-gradient(ellipse at center, #238636 0%, #196c2e 70%, #0e4421 100%);border:3px solid #f0f6fc;border-radius:6px;position:relative;overflow:hidden;margin-bottom:8px;box-shadow:inset 0 0 40px rgba(0,0,0,0.6)}
.pitch-lines{position:absolute;inset:0;pointer-events:none}
.pitch-lines::before{content:'';position:absolute;top:0;bottom:0;left:50%;width:2px;background:rgba(255,255,255,0.6);transform:translateX(-50%)}
.pitch-lines::after{content:'';position:absolute;top:50%;left:50%;width:70px;height:70px;border:2px solid rgba(255,255,255,0.6);border-radius:50%;transform:translate(-50%,-50%)}
.goal-left{position:absolute;left:0;top:32%;bottom:32%;width:15px;border:2px solid rgba(255,255,255,0.8);border-left:0}
.goal-right{position:absolute;right:0;top:32%;bottom:32%;width:15px;border:2px solid rgba(255,255,255,0.8);border-right:0}
.ball{position:absolute;width:12px;height:12px;background:#fff;border-radius:50%;transform:translate(-50%,-50%);transition:all 0.25s ease-in-out;box-shadow:0 0 10px #fff;z-index:6;border:1px solid #333}
</style></head><body>
<header><a class="logo" href="/">⚡ DEVICE {{device_number}}</a><div class="balance" id="bal">UGX {{ "%.2f"|format(balance) }}</div></header>
<div class="container">
<div class="card" id="timer">Loading...</div>
<div class="card"><div id="match" style="font-weight:bold;font-size:1.05rem;margin-bottom:10px;color:#58a6ff">Loading Match...</div>
  <div class="title">Full Time Result (1X2)</div>
  <div class="grid">
    <button class="odd market-option" id="ft_1" onclick="pickMarket('ft_result','1')">1 (Home)<br><span id="o1">-</span></button>
    <button class="odd market-option" id="ft_X" onclick="pickMarket('ft_result','X')">X (Draw)<br><span id="ox">-</span></button>
    <button class="odd market-option" id="ft_2" onclick="pickMarket('ft_result','2')">2 (Away)<br><span id="o2">-</span></button>
  </div>
  <div class="title" style="margin-top:12px">Half Time Result (1X2)</div>
  <div class="grid">
    <button class="odd market-option" id="ht_1" onclick="pickMarket('ht_result','1')">HT 1 (Home)<br><span id="ht_o1">-</span></button>
    <button class="odd market-option" id="ht_X" onclick="pickMarket('ht_result','X')">HT X (Draw)<br><span id="ht_ox">-</span></button>
    <button class="odd market-option" id="ht_2" onclick="pickMarket('ht_result','2')">HT 2 (Away)<br><span id="ht_o2">-</span></button>
  </div>
</div>
</div>
<div class="bet">
  <div id="sel" style="font-size:0.85rem;color:#8b949e;margin-bottom:4px">No selection made</div>
  <input id="stake" class="input" type="number" min="100" max="50000" placeholder="Stake UGX (100 - 50,000)" oninput="calc()">
  <div id="pay" style="color:{{color}};margin-top:4px;font-size:0.9rem">Potential Payout: UGX 0.00</div>
  <button id="place" class="place" onclick="bet()">PLACE BET</button>
</div>
<div id="live" class="live">
  <div class="tv-header">
    <div>
      <div id="tv-teams" class="tv-teams">Home vs Away</div>
      <div id="tv-status" class="tv-status">Live Match Stream • 1'</div>
    </div>
    <div id="tv-score" class="tv-scorebox">0 - 0</div>
  </div>
  <div class="pitch">
    <div class="pitch-lines"></div>
    <div class="goal-left"></div>
    <div class="goal-right"></div>
    <div id="ball" class="ball" style="left:50%;top:50%"></div>
  </div>
</div>
<script>
const id="{{device_id}}";
let sel=null, odds={}, htOdds={}, round=-1, betPlaced=false;\nlet myRoundBets=0;

setInterval(sync, 1000);

function sync(){
  fetch('/get_state')
    .then(r => {
      if(!r.ok) throw new Error("HTTP error " + r.status);
      return r.json();
    })
    .then(d => {
      document.getElementById('bal').innerText = 'UGX ' + (d.balance || 0).toFixed(2);
      
      if(d.round_idx !== round){
        round = d.round_idx;
        betPlaced = false;
        myRoundBets = 0;
        sel = null;
        document.querySelectorAll('.market-option').forEach(x=>x.classList.remove('selected'));
        document.getElementById('stake').value = '';
        document.getElementById('stake').disabled = false;
        document.getElementById('sel').innerText = 'No selection made';
        document.getElementById('pay').innerText = 'Potential Payout: UGX 0.00';
        document.getElementById('live').style.display = 'none';
      }
      
      odds = d.odds || {};
      htOdds = d.ht_odds || {};
      
      const homeName = d.home || "Home";
      const awayName = d.away || "Away";
      document.getElementById('match').innerText = homeName + ' vs ' + awayName;
      
      document.getElementById('o1').innerText = odds['1'] || '-';
      document.getElementById('ox').innerText = odds['X'] || '-';
      document.getElementById('o2').innerText = odds['2'] || '-';
      
      document.getElementById('ht_o1').innerText = htOdds['1'] || '-';
      document.getElementById('ht_ox').innerText = htOdds['X'] || '-';
      document.getElementById('ht_o2').innerText = htOdds['2'] || '-';
      
      let t = document.getElementById('timer'), p = document.getElementById('place');
      if(d.phase === 'betting'){
        document.getElementById('live').style.display = 'none';
        t.innerText = '⏱️ Betting closes in ' + (d.time_left || 0).toFixed(1) + 's';
        p.disabled = false;
        p.innerText = myRoundBets > 0 ? 'PLACE ANOTHER BET' : 'PLACE BET';
      } else {
        t.innerText = '🚨 STREAM LIVE — MATCH IN PROGRESS';
        p.disabled = true;
        p.innerText = 'STREAM ACTIVE';
        live(d);
      }
    })
    .catch(err => {
      console.warn("Sync temporarily unavailable:", err);
    });
}

function getMarketName(market, type){
  const names = {'ft_result':{'1':'FT: Home (1)','X':'FT: Draw (X)','2':'FT: Away (2)'},'ht_result':{'1':'HT: Home (1)','X':'HT: Draw (X)','2':'HT: Away (2)'}};
  return (names[market] && names[market][type]) || type;
}

function pickMarket(market, type){
  if(!['ft_result','ht_result'].includes(market) || !['1','X','2'].includes(type)) return;
  sel = {market: market, type: type};
  document.querySelectorAll('.market-option').forEach(b => b.classList.remove('selected'));
  const prefix = (market === 'ht_result' ? 'ht_' : 'ft_');
  const btn = document.getElementById(prefix + type);
  if(btn) btn.classList.add('selected');
  const source = (market === 'ht_result' ? htOdds : odds);
  document.getElementById('sel').innerText = 'Selected: ' + getMarketName(market, type) + ' @ ' + (source[type] || 0);
  calc();
}

function calc(){
  let s = parseFloat(document.getElementById('stake').value || 0);
  let source = (sel && sel.market === 'ht_result') ? htOdds : odds;
  let o = sel ? (source[sel.type] || 0) : 0;
  document.getElementById('pay').innerText = 'Potential Payout: UGX ' + (sel ? (s * o) : 0).toFixed(2);
}

let betRequestInProgress = false;
async function bet(){
  const placeButton = document.getElementById('place');
  const stakeInput = document.getElementById('stake');
  const s = Number(stakeInput.value);
  if(betRequestInProgress) return;
  if(!sel) return alert('Please pick a market first!');
  if(!Number.isFinite(s) || s < 100 || s > 50000) return alert('Stake must be between UGX 100 and UGX 50,000!');

  betRequestInProgress = true;
  placeButton.disabled = true;
  placeButton.innerText = 'SENDING BET...';
  try {
    const response = await fetch('/place_bet', {
      method: 'POST',
      headers: {'Content-Type':'application/json', 'Accept':'application/json'},
      credentials: 'same-origin',
      body: JSON.stringify({device_id: id, stake: s, selection: {market: sel.market, type: sel.type}})
    });
    const d = await response.json().catch(() => ({}));
    if(!response.ok) throw new Error(d.message || ('Server returned HTTP ' + response.status));
    if(!d || typeof d.success !== 'boolean') throw new Error('The server returned an unreadable response. Please refresh and check your balance before trying again.');
    alert(d.message || (d.success ? 'Bet successfully placed!' : 'Bet was not accepted.'));
    if(d.success){
      myRoundBets += 1;
      betPlaced = false;
      document.getElementById('bal').innerText = 'UGX ' + Number(d.new_balance || 0).toFixed(2);
      placeButton.innerText = 'PLACE ANOTHER BET';
      stakeInput.value = '';
      sel = null;
      document.querySelectorAll('.market-option').forEach(b => b.classList.remove('selected'));
      document.getElementById('sel').innerText = 'Bet placed. Select another bet to continue.';
      document.getElementById('pay').innerText = 'Potential Payout: UGX 0.00';
      stakeInput.disabled = false;
    }
  } catch(err) {
    console.error('Bet submission failed:', err);
    alert('Bet could not be confirmed: ' + (err.message || 'Connection problem') + '. Check the balance and bet status before retrying.');
  } finally {
    betRequestInProgress = false;
    if(!betPlaced){
      stakeInput.disabled = false;
      placeButton.disabled = false;
      placeButton.innerText = 'PLACE BET';
    }
  }
}

function live(d){
  let v = document.getElementById('live');
  v.style.display = 'block';
  let el = Math.max(0, 40 - d.time_left);
  let min = Math.min(90, Math.floor(el / 40 * 90) + 1);
  
  document.getElementById('tv-teams').innerText = (d.home || "Home") + ' vs ' + (d.away || "Away");
  document.getElementById('tv-status').innerText = "Live Match Stream • " + min + "'";
  
  let hg = 0, ag = 0;
  if(min >= 90){
    hg = d.final_home_goals || 0;
    ag = d.final_away_goals || 0;
  } else if(Array.isArray(d.match_events)){
    d.match_events.filter(e => e.minute <= min).forEach(e => {
      if(e.type === 'goal'){
        if(e.side === 'home') hg++;
        else if(e.side === 'away') ag++;
      }
    });
  }
  
  document.getElementById('tv-score').innerText = hg + ' - ' + ag;
  let bx = 50 + Math.sin(min * 0.7) * 36;
  let by = 50 + Math.cos(min * 0.5) * 28;
  document.getElementById('ball').style.left = bx + '%';
  document.getElementById('ball').style.top = by + '%';
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
</div></body></html>
"""

# Initialize the persistent database when Gunicorn/Render imports this module.
# The previous version only initialized it under __main__, which caused HTTP 500s on Render.
init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5961, debug=False)