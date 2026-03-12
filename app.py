from flask import Flask, render_template, request, jsonify, session
import json
import os
import uuid
import urllib.request
import urllib.error
import urllib.parse
import base64
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'treatblocker-secret-2024')
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE']   = True
app.config['PERMANENT_SESSION_LIFETIME'] = 86400  # 24 hours

# ─────────────────────────────────────────
# CONFIG — fill these in after deploying
# ─────────────────────────────────────────
# Payment Provider Configuration (Stripe)
STRIPE_API_KEY       = os.environ.get('STRIPE_API_KEY', 'sk_test_51234567890')
STRIPE_API           = 'https://api.stripe.com/v1'
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
    'pro':    {'name': 'Pro',    'price': 4.99,  'currency': 'USD'},
    'family': {'name': 'Family', 'price': 9.99,  'currency': 'USD'},
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
# STRIPE — CREATE CHECKOUT SESSION
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

    # Prepare payload for Stripe Checkout Session API
    payload = urllib.parse.urlencode({
        'payment_method_types[]': 'card',
        'line_items[0][price_data][currency]': plan['currency'].lower(),
        'line_items[0][price_data][unit_amount]': int(plan['price'] * 100),  # Convert to cents
        'line_items[0][price_data][product_data][name]': f"TreatBlocker {plan['name']} Plan",
        'line_items[0][quantity]': '1',
        'mode': 'payment',
        'success_url': f"{SITE_URL}/dashboard?session_id={{CHECKOUT_SESSION_ID}}",
        'cancel_url': f"{SITE_URL}/#pricing",
        'metadata[plan]': plan_id,
        'metadata[user_id]': user_id,
        'metadata[order_id]': f"{user_id}:{plan_id}:{uuid.uuid4().hex[:8]}"
    }).encode('utf-8')

    endpoint = f"{STRIPE_API}/checkout/sessions"
    
    # Create Basic Auth header for Stripe
    auth_string = base64.b64encode(f"{STRIPE_API_KEY}:".encode()).decode()
    
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            'Authorization': f'Basic {auth_string}',
            'Content-Type': 'application/x-www-form-urlencoded',
            'User-Agent': 'TreatBlocker/1.0'
        },
        method='POST'
    )

    try:
        print(f'[CHECKOUT] Making Stripe request to {endpoint}')
        print(f'[CHECKOUT] Plan: {plan_id}, User: {user_id}')
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            print(f'[CHECKOUT] Stripe response: {json.dumps(result)}')

        # Stripe returns the session object with a url field
        if result.get('url'):
            print(f'[CHECKOUT] Success - returning checkout URL: {result["url"]}')
            return jsonify({
                'success':  True,
                'payLink':  result.get('url'),
                'sessionId': result.get('id'),
                'plan':     plan_id
            })
        else:
            print(f'[CHECKOUT] No URL in Stripe response: {result}')
            return jsonify({'error': 'Failed to create checkout session', 'debug': result}), 400

    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        print(f'[CHECKOUT] HTTP Error {e.code}: {error_body}')
        return jsonify({'error': f'Payment provider error: {error_body}'}), 500
    except Exception as e:
        print(f'[CHECKOUT] Exception: {str(e)}')
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ─────────────────────────────────────────
# STRIPE — WEBHOOK
# ─────────────────────────────────────────

@app.route('/api/webhook/stripe', methods=['POST'])
def stripe_webhook():
    data = request.get_json(silent=True) or {}
    
    event_type = data.get('type')
    event_data = data.get('data', {}).get('object', {})
    
    if event_type == 'checkout.session.completed':
        metadata = event_data.get('metadata', {})
        user_id = metadata.get('user_id')
        plan_id = metadata.get('plan')
        
        if user_id and plan_id and plan_id in ('pro', 'family'):
            subscriptions_db[user_id] = {
                'plan':    plan_id,
                'expires': (datetime.now() + timedelta(days=30)).isoformat()
            }
            print(f'[WEBHOOK] Subscription activated for user {user_id}: {plan_id}')

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
