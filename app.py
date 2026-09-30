import os
import re
import unicodedata
from functools import wraps
from typing import Any

from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    import firebase_admin
    from firebase_admin import auth, credentials, db
except ImportError:  # pragma: no cover - allows local API scaffolding without Firebase
    firebase_admin = None
    auth = credentials = db = None

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024
allowed_origins = [x.strip() for x in os.getenv('CORS_ORIGINS', 'http://localhost:8080').split(',') if x.strip()]
CORS(app, resources={r'/api/*': {'origins': allowed_origins}})


def normalize_name(value: str) -> str:
    value = unicodedata.normalize('NFD', str(value or ''))
    value = ''.join(ch for ch in value if unicodedata.category(ch) != 'Mn')
    value = value.lower()
    value = re.sub(r'[^a-z0-9\s]', ' ', value)
    return re.sub(r'\s+', ' ', value).strip()


def normalize_dni(value: str) -> str:
    return re.sub(r'\D', '', str(value or ''))


def firebase_ready() -> bool:
    return firebase_admin is not None and bool(firebase_admin._apps)


def init_firebase() -> None:
    if not firebase_admin or firebase_admin._apps:
        return
    service_json = os.getenv('FIREBASE_SERVICE_ACCOUNT_JSON')
    database_url = os.getenv('FIREBASE_DATABASE_URL')
    if not service_json or not database_url:
        return
    import json
    firebase_admin.initialize_app(credentials.Certificate(json.loads(service_json)), {'databaseURL': database_url})


init_firebase()


def require_admin(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        if not firebase_ready():
            return jsonify({'error': 'backend_not_configured'}), 503
        header = request.headers.get('Authorization', '')
        if not header.startswith('Bearer '):
            return jsonify({'error': 'unauthorized'}), 401
        try:
            decoded = auth.verify_id_token(header[7:].strip())
            if not db.reference(f"admins/{decoded['uid']}").get():
                return jsonify({'error': 'forbidden'}), 403
        except Exception:
            return jsonify({'error': 'unauthorized'}), 401
        return handler(*args, **kwargs)
    return wrapped


def public_records(season: str) -> list[dict[str, Any]]:
    if not firebase_ready():
        return []
    records = db.reference(f'registrosPorTemporada/{season}').get() or {}
    result = []
    for item in records.values() if isinstance(records, dict) else []:
        dni = item.get('_dni') or item.get('DNI')
        if not dni:
            continue
        player = (db.reference(f'jugadores/{dni}/datosPersonales').get() or {})
        result.append({
            'playerId': str(dni),
            'displayName': player.get('NOMBRE') or item.get('NOMBRE') or '',
            'category': item.get('CATEGORIA', ''),
            'team': item.get('EQUIPO', ''),
        })
    unique = {x['playerId']: x for x in result if x['displayName']}
    return list(unique.values())


@app.get('/health')
def health():
    return jsonify({'status': 'ok', 'firebaseConfigured': firebase_ready()})


@app.get('/api/v1/public/letter-players')
def letter_players():
    season = request.args.get('season', '').strip()
    query = normalize_name(request.args.get('search', ''))
    if not season or len(query) < 2:
        return jsonify([])
    query_tokens = query.split()
    matches = []
    for item in public_records(season):
        candidate = normalize_name(item['displayName'])
        tokens = candidate.split()
        if query == candidate:
            score = 1000
        elif all(any(t == c or c.startswith(t) for c in tokens) for t in query_tokens):
            score = 600 + len(query_tokens)
        elif candidate.startswith(query) or query in candidate:
            score = 300
        else:
            continue
        matches.append((score, item))
    matches.sort(key=lambda x: (-x[0], x[1]['displayName']))
    return jsonify([item for _, item in matches[:10]])


@app.post('/api/v1/public/letters/verify')
def verify_letter():
    payload = request.get_json(silent=True) or {}
    season = str(payload.get('season', '')).strip()
    player_id = str(payload.get('playerId', '')).strip()
    dni = normalize_dni(payload.get('dni', ''))
    if not season or not player_id or not dni:
        return jsonify({'error': 'No se pudo verificar la identidad con los datos ingresados.'}), 400
    # The real lookup is intentionally kept server-side. No DNI is returned.
    if not firebase_ready():
        return jsonify({'error': 'backend_not_configured'}), 503
    personal = db.reference(f'jugadores/{player_id}/datosPersonales').get() or {}
    records = public_records(season)
    valid = any(x['playerId'] == player_id for x in records) and normalize_dni(personal.get('DNI', player_id)) == dni
    if not valid:
        return jsonify({'error': 'No se pudo verificar la identidad con los datos ingresados.'}), 403
    return jsonify({'letter': {'displayName': personal.get('NOMBRE', ''), 'clubName': 'Defensor Sporting Club', 'season': season}})


@app.get('/api/v1/public/medical-status')
def medical_status():
    # Public endpoint intentionally returns only public sporting eligibility fields.
    player_id = request.args.get('playerId', '').strip()
    if not player_id or not firebase_ready():
        return jsonify({'error': 'not_found'}), 404
    personal = db.reference(f'jugadores/{player_id}/datosPersonales').get() or {}
    if not personal:
        return jsonify({'error': 'not_found'}), 404
    return jsonify({'playerId': player_id, 'eligibleUntil': personal.get('FM Hasta', ''), 'status': 'public'})


@app.post('/api/v1/admin/medical-status')
@require_admin
def update_medical_status():
    payload = request.get_json(silent=True) or {}
    player_id = str(payload.get('playerId', '')).strip()
    until = str(payload.get('eligibleUntil', '')).strip()
    if not re.fullmatch(r'\d{1,2}/\d{1,2}/\d{4}', until):
        return jsonify({'error': 'invalid_date'}), 400
    db.reference(f'jugadores/{player_id}/datosPersonales/FM Hasta').set(until)
    return jsonify({'ok': True})


if __name__ == '__main__':
    init_firebase()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '8000')))
