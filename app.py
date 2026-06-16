import os
import uuid
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
import psycopg2
import psycopg2.extras
import resend

app = Flask(__name__)

# Configurações (serão preenchidas pelas variáveis de ambiente do Render)
DATABASE_URL = os.getenv("DATABASE_URL")          # "DATABASE_URL" é o nome da variável
RESEND_API_KEY = os.getenv("RESEND_API_KEY")      # "RESEND_API_KEY" é o nome da variável
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN")
if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY

# ---------- BANCO DE DADOS ----------
def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS licenses (
            license_key   TEXT PRIMARY KEY,
            email         TEXT NOT NULL,
            plan          TEXT NOT NULL CHECK (plan IN ('monthly','yearly','lifetime')),
            is_active     BOOLEAN DEFAULT TRUE,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at    TIMESTAMP,
            max_activations INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS activations (
            id           SERIAL PRIMARY KEY,
            license_key  TEXT REFERENCES licenses(license_key),
            hwid         TEXT NOT NULL,
            activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(license_key, hwid)
        );
        CREATE INDEX IF NOT EXISTS idx_activations_license ON activations(license_key);
    """)
    conn.commit()
    conn.close()

# ---------- E-MAIL ----------
def send_license_email(to_email, license_key, plan):
    if not RESEND_API_KEY:
        print("Resend API Key não configurada. E-mail não enviado.")
        return
    plan_names = {
        'monthly': 'Mensal',
        'yearly': 'Anual',
        'lifetime': 'Vitalícia'
    }
    subject = f"Sua licença DANFS-e PDF ({plan_names.get(plan, '')})"
    html = f"""
    <h1>Obrigado por adquirir o DANFS-e PDF!</h1>
    <p>Plano: <strong>{plan_names.get(plan, plan)}</strong></p>
    <p>Sua chave de licença:</p>
    <h2 style="background:#f0f0f0;padding:10px;border-radius:5px;">{license_key}</h2>
    <p>Baixe o programa em: <a href="https://crtlaistudios.top/DANFSePDF/">crtlaistudios.top/DANFSePDF/</a></p>
    <br>
    <p style="color:#888; font-size:12px;">Este é um e-mail automático. Por favor, não responda a esta mensagem.</p>
    """
    params = {
        "from": "DANFS-e PDF <suporte@crtlaistudios.top>",
        "to": [to_email],
        "subject": subject,
        "html": html,
        "reply_to": "noreply@crtlaistudios.top"  
    }
    r = resend.Emails.send(params)
    print(f"E-mail enviado para {to_email}: {r}")

# ---------- WEBHOOK KIWIFY ----------
@app.route('/webhook/kiwify', methods=['POST'])
def kiwify_webhook():
    # 1. VALIDAÇÃO DO TOKEN
    token = request.args.get('token')
    if token != WEBHOOK_TOKEN:
        return jsonify({"error": "Acesso não autorizado. Token inválido."}), 401

    data = request.json
    if not data:
        return jsonify({"error": "Payload vazio"}), 400

    print("--- NOVO PAYLOAD RECEBIDO DO KIWIFY ---")
    print(data)
    print("---------------------------------------")

    # 2. EXTRAÇÃO DOS DADOS
    order_status = (data.get('order_status') or data.get('status', '')).lower()

    customer_data = data.get('Customer') or data.get('customer') or {}
    email = customer_data.get('email') or data.get('customer_email')

    product_data = data.get('Product') or data.get('product') or {}
    # Campo correto: 'product_id' (não 'id')
    product_id = str(product_data.get('product_id') or data.get('product_id', ''))

    if not email:
        return jsonify({"error": "Email do comprador não encontrado"}), 400

    # 3. MAPEAMENTO DE PLANOS (substitua pelos seus IDs reais)
    plan_map = {
        'be7f32a0-6960-11f1-898f-bfa154be5a31': 'monthly',
        '5f9b49c0-6962-11f1-846b-d3e350854580': 'yearly',
        'c51001d0-6965-11f1-bcb7-c7a45546616b': 'lifetime'
    }
    plan = plan_map.get(product_id, 'monthly')
    max_activations = 2 if plan == 'lifetime' else 1

    # 4. AÇÃO CONFORME STATUS
    if order_status in ['paid', 'approved', 'renewed']:   # <-- adicionado 'renewed'
        license_key = str(uuid.uuid4())
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO licenses (license_key, email, plan, max_activations, is_active)
                VALUES (%s, %s, %s, %s, TRUE)
            """, (license_key, email, plan, max_activations))
            conn.commit()
            conn.close()
            send_license_email(email, license_key, plan)
            return jsonify({"status": "success", "action": "license_processed"}), 200
        except Exception as e:
            return jsonify({"error": f"Erro no banco: {str(e)}"}), 500

    elif order_status in ['refunded', 'chargeback', 'canceled', 'subscription_canceled']:
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("UPDATE licenses SET is_active = FALSE WHERE email = %s", (email,))
            conn.commit()
            conn.close()
            return jsonify({"status": "success", "action": "license_revoked"}), 200
        except Exception as e:
            return jsonify({"error": f"Erro ao revogar: {str(e)}"}), 500

    else:
        return jsonify({"status": "ignored", "reason": f"Status {order_status} não requer ação"}), 200

# ---------- VALIDAÇÃO ----------
@app.route('/validate', methods=['POST'])
def validate_license():
    data = request.json
    license_key = data.get('license_key')
    hwid = data.get('hwid')

    if not license_key or not hwid:
        return jsonify({"valid": False, "reason": "missing_data"}), 400

    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        cur.execute("SELECT * FROM licenses WHERE license_key = %s AND is_active = TRUE", (license_key,))
        lic = cur.fetchone()
        if not lic:
            conn.close()
            return jsonify({"valid": False, "reason": "invalid"}), 401

        if lic['plan'] != 'lifetime' and lic['expires_at'] and lic['expires_at'] < datetime.utcnow():
            conn.close()
            return jsonify({"valid": False, "reason": "expired"}), 401

        cur.execute("SELECT 1 FROM activations WHERE license_key = %s AND hwid = %s", (license_key, hwid))
        if cur.fetchone():
            conn.close()
            return jsonify({
                "valid": True,
                "plan": lic['plan'],
                "expires_at": lic['expires_at'].isoformat() if lic['expires_at'] else None
            }), 200

        cur.execute("SELECT COUNT(*) as cnt FROM activations WHERE license_key = %s", (license_key,))
        count = cur.fetchone()['cnt']
        if count >= lic['max_activations']:
            conn.close()
            return jsonify({"valid": False, "reason": "max_activations"}), 401

        if lic['plan'] == 'monthly':
            expires = datetime.utcnow() + timedelta(days=30)
        elif lic['plan'] == 'yearly':
            expires = datetime.utcnow() + timedelta(days=365)
        else:
            expires = None

        cur.execute("INSERT INTO activations (license_key, hwid) VALUES (%s, %s)", (license_key, hwid))
        if count == 0 and expires:
            cur.execute("UPDATE licenses SET expires_at = %s WHERE license_key = %s", (expires, license_key))
        conn.commit()
        conn.close()

        return jsonify({
            "valid": True,
            "plan": lic['plan'],
            "expires_at": expires.isoformat() if expires else None
        }), 200

    except Exception as e:
        return jsonify({"valid": False, "reason": str(e)}), 500

# ---------- INÍCIO ----------
if __name__ == '__main__':
    init_db()
    port = int(os.getenv("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
