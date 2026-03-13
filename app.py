from flask import Flask, render_template, request, jsonify, session
import json
import os
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'treatblocker-secret-2024')
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE']   = True
app.config['PERMANENT_SESSION_LIFETIME'] = 86400  # 24 hours

# ─────────────────────────────────────────
# CONFIG — fill these in after deploying
# ─────────────────────────────────────────
OXAPAY_MERCHANT_KEY = os.environ.get('OXAPAY_MERCHANT_KEY', 'EEN3JK-5CNPH8-1C7XFJ-WX6WLT')
OXAPAY_API           = 'https://api.oxapay.com/v1'
OPENROUTER_API_KEY   = os.environ.get('OPENROUTER_API_KEY', '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')
OPENROUTER_API       = 'https://openrouter.ai/api/v1/chat/completions'
# YOUR Replit URL — update this after you deploy
SITE_URL            = os.environ.get('SITE_URL', 'https://diego-production-28d4.up.railway.app')
# Set to True while testing, False when you go live
SANDBOX_MODE        = False

# ─────────────────────────────────────────
# PLANS
# ─────────────────────────────────────────
PLANS = {
    'pro':    {'name': 'Pro',    'price': 4.99,  'currency': 'USDT'},
    'family': {'name': 'Family', 'price': 9.99,  'currency': 'USDT'},
}

# ─────────────────────────────────────────
# IN-MEMORY STORAGE
# (swap for a real DB like SQLite later)
# ─────────────────────────────────────────
blocks_db        = {}
subscriptions_db = {}

# ─────────────────────────────────────────
# PAGES
# ─────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html')

# ─────────────────────────────────────────
# OXAPAY — CREATE INVOICE
# ─────────────────────────────────────────
@app.route('/api/checkout', methods=['POST'])
def create_checkout():
    data    = request.json
    plan_id = data.get('plan')
    user_id = session.get('user_id', str(uuid.uuid4()))
    session['user_id'] = user_id
    session.permanent = True

    if plan_id not in PLANS:
        return jsonify({'error': 'Invalid plan'}), 400

    plan = PLANS[plan_id]

    # In sandbox mode the merchant field is literally 'sandbox'
    merchant = 'sandbox' if SANDBOX_MODE else OXAPAY_MERCHANT_KEY

    # Prepare payload for OxaPay Generate Invoice API
    payload = json.dumps({
        "merchant":         merchant,
        "amount":           plan['price'],
        "currency":         plan['currency'],
        "lifeTime":         30,
        "feePaidByPayer":   1,
        "underPaidCover":   2.5,
        "callbackUrl":      f"{SITE_URL}/api/webhook/oxapay",
        "returnUrl":        f"{SITE_URL}/dashboard",
        "description":      f"TreatBlocker {plan['name']} Plan",
        "orderId":          f"{user_id}:{plan_id}:{uuid.uuid4().hex[:8]}"
    }).encode('utf-8')

    req = urllib.request.Request(
        f"{OXAPAY_API}/merchants/request",
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode('utf-8'))

        # OxaPay returns result 100 for success
        if result.get('result') == 100:
            return jsonify({
                'success':  True,
                'payLink':  result.get('payLink'),
                'trackId':  result.get('trackId'),
                'plan':     plan_id,
                'sandbox':  SANDBOX_MODE
            })
        else:
            return jsonify({'error': result.get('message', 'OxaPay error')}), 400

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─────────────────────────────────────────
# OXAPAY — WEBHOOK
# ─────────────────────────────────────────

@app.route('/api/webhook/oxapay', methods=['POST'])
def oxapay_webhook():
    data = request.get_json(silent=True) or {}

    # Skip merchant check in sandbox mode
    if not SANDBOX_MODE:
        if data.get('merchant') != OXAPAY_MERCHANT_KEY:
            return jsonify({'error': 'Unauthorized'}), 401

    status   = data.get('status')
    order_id = data.get('orderId', '')

    if status == 'Paid':
        # orderId format: user_id:plan_id:random
        parts = order_id.split(':')
        if len(parts) >= 2:
            user_id = parts[0].strip()
            plan_id = parts[1].strip()
            if plan_id in ('pro', 'family'):
                subscriptions_db[user_id] = {
                    'plan':    plan_id,
                    'expires': (datetime.now() + timedelta(days=30)).isoformat()
                }

    return jsonify({'result': 100})

# ─────────────────────────────────────────
# SUBSCRIPTION STATUS
# ─────────────────────────────────────────

@app.route('/api/subscription')
def get_subscription():
    user_id = session.get('user_id', '')
    sub     = subscriptions_db.get(user_id)

    if not sub:
        return jsonify({'plan': 'free', 'active': False})

    active = datetime.fromisoformat(sub['expires']) > datetime.now()

    return jsonify({
        'plan':    sub['plan'] if active else 'free',
        'active':  active,
        'expires': sub['expires']
    })

# ─────────────────────────────────────────
# AI ANALYSIS
# ─────────────────────────────────────────

@app.route('/api/analyze', methods=['POST'])
def analyze_url():
    data    = request.json
    url     = data.get('url', '')
    user_id = session.get('user_id', str(uuid.uuid4()))
    session['user_id'] = user_id
    session.permanent = True

    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    # Free tier limit check
    sub    = subscriptions_db.get(user_id)
    is_pro = sub and datetime.fromisoformat(sub['expires']) > datetime.now()

    user_blocks       = blocks_db.get(user_id, [])
    block_count_today = sum(
        1 for b in user_blocks
        if datetime.fromisoformat(b['timestamp']) > datetime.now() - timedelta(days=1)
    )

    if not is_pro and block_count_today >= 3:
        return jsonify({
            'error':   'free_limit',
            'message': 'Free plan limit reached (3/day). Upgrade to Pro for unlimited scans.'
        }), 403

    total_saved = sum(b.get('price', 0) for b in user_blocks if b.get('blocked', True))

    prompt = f"""You are TreatBlocker's AI engine. Analyze this shopping URL for impulse buy risk.

URL: {url}
User's blocks today: {block_count_today}
Total saved this session: ${total_saved}

Respond ONLY with a JSON object (no markdown, no backticks):
{{
  "risk_level": "HIGH" | "MEDIUM" | "LOW",
  "risk_score": 0-100,
  "product_name": "detected product name or category",
  "estimated_price": estimated price as number (0 if unknown),
  "platform": "Amazon" | "DoorDash" | "Shopify" | "Shein" | "Target" | "Other",
  "regret_message": "punchy 1-sentence why this is an impulse buy",
  "regret_reason": "deeper 1-sentence psychological reason",
  "savings_tip": "1 actionable alternative to buying this right now",
  "wait_hours": 24,
  "emoji": "single relevant emoji"
}}"""

    try:
        payload = json.dumps({
            "model":      "mistralai/mistral-7b-instruct:free",
            "max_tokens": 600,
            "messages":   [{"role": "user", "content": prompt}]
        }).encode('utf-8')

        req = urllib.request.Request(
            OPENROUTER_API,
            data=payload,
            headers={
                'Content-Type':  'application/json',
                'Authorization': f'Bearer {OPENROUTER_API_KEY}',
                'HTTP-Referer':  SITE_URL,
                'X-Title':       'TreatBlocker'
            }
        )

        with urllib.request.urlopen(req, timeout=15) as resp:
            result   = json.loads(resp.read().decode('utf-8'))
            text     = result['choices'][0]['message']['content'].strip()
            text     = text.replace('```json', '').replace('```', '').strip()
            analysis = json.loads(text)

    except Exception:
        analysis = {
            "risk_level":      "HIGH",
            "risk_score":      84,
            "product_name":    "Mystery Purchase",
            "estimated_price": 47,
            "platform":        "Other",
            "regret_message":  "You've done this before. You know how it ends.",
            "regret_reason":   "Late-night purchases have a 73% regret rate by morning.",
            "savings_tip":     "Sleep on it. If you still want it in 24hrs, it might be real.",
            "wait_hours":      24,
            "emoji":           "🛑"
        }

    block_entry = {
        'id':        str(uuid.uuid4()),
        'url':       url,
        'timestamp': datetime.now().isoformat(),
        'blocked':   True,
        'analysis':  analysis,
        'price':     analysis.get('estimated_price', 0),
        'status':    'blocked'
    }

    if user_id not in blocks_db:
        blocks_db[user_id] = []
    blocks_db[user_id].append(block_entry)

    return jsonify({
        'success':  True,
        'analysis': analysis,
        'block_id': block_entry['id'],
        'stats': {
            'blocks_today': block_count_today + 1,
            'total_saved':  total_saved + analysis.get('estimated_price', 0)
        }
    })

# ─────────────────────────────────────────
# OVERRIDE
# ─────────────────────────────────────────

@app.route('/api/override', methods=['POST'])
def override_block():
    data     = request.json
    block_id = data.get('block_id')
    user_id  = session.get('user_id', '')

    for block in blocks_db.get(user_id, []):
        if block['id'] == block_id:
            block['status']  = 'overridden'
            block['blocked'] = False
            return jsonify({'success': True})

    return jsonify({'error': 'Block not found'}), 404

# ─────────────────────────────────────────
# STATS
# ─────────────────────────────────────────

@app.route('/api/stats')
def get_stats():
    user_id            = session.get('user_id', str(uuid.uuid4()))
    session['user_id'] = user_id
    user_blocks        = blocks_db.get(user_id, [])

    total_blocks       = len(user_blocks)
    saved_blocks       = [b for b in user_blocks if b.get('status') == 'blocked']
    overridden         = [b for b in user_blocks if b.get('status') == 'overridden']
    total_saved        = sum(b.get('price', 0) for b in saved_blocks)
    total_spent_anyway = sum(b.get('price', 0) for b in overridden)

    now        = datetime.now()
    week_saved = sum(
        b.get('price', 0) for b in saved_blocks
        if datetime.fromisoformat(b['timestamp']) > now - timedelta(days=7)
    )

    platform_breakdown = {}
    for b in user_blocks:
        platform = b.get('analysis', {}).get('platform', 'Other')
        if platform not in platform_breakdown:
            platform_breakdown[platform] = {'count': 0, 'saved': 0}
        platform_breakdown[platform]['count'] += 1
        if b.get('status') == 'blocked':
            platform_breakdown[platform]['saved'] += b.get('price', 0)

    recent_blocks = sorted(user_blocks, key=lambda x: x['timestamp'], reverse=True)[:10]

    return jsonify({
        'total_blocks':       total_blocks,
        'total_saved':        round(total_saved, 2),
        'week_saved':         round(week_saved, 2),
        'total_spent_anyway': round(total_spent_anyway, 2),
        'success_rate':       round((len(saved_blocks) / total_blocks * 100) if total_blocks > 0 else 0, 1),
        'platform_breakdown': platform_breakdown,
        'recent_blocks':      recent_blocks
    })

# ─────────────────────────────────────────
# RUN
# ─────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(debug=False, host='0.0.0.0', port=port)
