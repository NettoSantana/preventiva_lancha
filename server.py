from flask import Flask, jsonify, send_from_directory, request
import time, hashlib, requests, os, math, json, uuid

app = Flask(__name__)

# =========================
#  BRASILSAT (API)
# =========================
BASE_URL = "https://gps.brasilsatgps.com.br"
ACCOUNT  = os.getenv("BRASILSAT_ACCOUNT", "nettosantana@icloud.com")
PASSWORD = os.getenv("BRASILSAT_PASSWORD", "1234567")

def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def get_token():
    now = int(time.time())
    signature = md5(md5(PASSWORD) + str(now))
    url = f"{BASE_URL}/api/authorization"
    r = requests.get(url, params={"time": now, "account": ACCOUNT, "signature": signature}, timeout=10)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 0:
        raise RuntimeError(f"Auth falhou: {j}")
    return j["record"]["access_token"]

def track(access_token: str, imei: str):
    url = f"{BASE_URL}/api/track"
    r = requests.get(url, params={"access_token": access_token, "imeis": imei}, timeout=10)
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 0:
        raise RuntimeError(f"Track falhou: {j}")
    return j["record"][0]

# =========================
#  "DB" SIMPLES EM JSON
# =========================
DB_FILE = os.path.join(os.path.dirname(__file__), "db.json")

DEFAULT_PLANO_HORAS = [
    {"nome": "Troca de óleo do motor", "unidade": "hora", "primeira_execucao": 100, "intervalo": 100, "avisar_antes": 10},
    {"nome": "Troca do filtro de óleo", "unidade": "hora", "primeira_execucao": 100, "intervalo": 100, "avisar_antes": 10},
    {"nome": "Drenar separador de água/combustível", "unidade": "hora", "primeira_execucao": 100, "intervalo": 100, "avisar_antes": 10},
    {"nome": "Troca do filtro de combustível", "unidade": "hora", "primeira_execucao": 200, "intervalo": 200, "avisar_antes": 10},
]

DEFAULT_PLANO_KM = [
    {"nome": "Troca de óleo do motor", "unidade": "km", "primeira_execucao": 10000, "intervalo": 10000, "avisar_antes": 500},
    {"nome": "Troca do filtro de óleo", "unidade": "km", "primeira_execucao": 10000, "intervalo": 10000, "avisar_antes": 500},
    {"nome": "Troca do filtro de combustível", "unidade": "km", "primeira_execucao": 20000, "intervalo": 20000, "avisar_antes": 1000},
]

def load_db():
    if not os.path.exists(DB_FILE):
        return {"ativos": [], "ativo_atual_id": None}
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"ativos": [], "ativo_atual_id": None}

def save_db(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

db = load_db()

def get_ativo_atual():
    ativos = db.get("ativos", [])
    if not ativos:
        return None
    ativo_id = db.get("ativo_atual_id")
    if ativo_id:
        for a in ativos:
            if a["id"] == ativo_id:
                return a
    # fallback: primeiro
    db["ativo_atual_id"] = ativos[0]["id"]
    save_db(db)
    return ativos[0]

def ensure_defaults_for_ativo(ativo):
    if "plano_preventivo" not in ativo or not isinstance(ativo["plano_preventivo"], list):
        ativo["plano_preventivo"] = DEFAULT_PLANO_HORAS if ativo["medida_base"] == "hora" else DEFAULT_PLANO_KM

# =========================
#  LÓGICA DE PREVENTIVA
# =========================
def calcular_status_preventiva(uso_ajustado: float, plano: list):
    tarefas = []
    for item in plano:
        unidade = item.get("unidade", "hora")
        primeira = float(item.get("primeira_execucao", 0))
        intervalo = float(item.get("intervalo", 0))
        avisar_antes = float(item.get("avisar_antes", 10))

        if intervalo <= 0 or primeira <= 0:
            continue

        if uso_ajustado < primeira:
            proxima = primeira
        else:
            ciclos = math.floor((uso_ajustado - primeira) / intervalo)
            proxima = primeira + (ciclos + 1) * intervalo

        faltam = round(proxima - uso_ajustado, 2)

        if faltam <= 0:
            status = "ATRASADO"
        elif faltam <= avisar_antes:
            status = "ATENCAO"
        else:
            status = "OK"

        tarefas.append({
            "nome": item.get("nome", ""),
            "unidade": unidade,
            "primeira_execucao": primeira,
            "intervalo": intervalo,
            "avisar_antes": avisar_antes,
            "proxima_execucao": round(proxima, 2),
            "faltam": faltam,
            "status": status
        })

    prioridade = {"ATRASADO": 0, "ATENCAO": 1, "OK": 2}
    tarefas.sort(key=lambda x: (prioridade[x["status"]], x["faltam"]))
    return tarefas

def obter_dados_brasilsat_por_imei(imei: str):
    token = get_token()
    data = track(token, imei)

    acctime_s = int(data.get("acctime") or 0)
    horas_reais = round(acctime_s / 3600.0, 2)

    # mileage vem em METROS. km_total = mileage / 1000
    mileage_m = float(data.get("mileage") or 0)
    km_total = round(mileage_m / 1000.0, 2)

    return {
        "imei": data.get("imei"),
        "motor_ligado": bool(int(data.get("accstatus") or 0)),
        "horas_reais": horas_reais,
        "km_reais": km_total,
        "tensao_bateria": float(data.get("externalpower") or 0),
        "servertime": data.get("servertime"),
    }

# =========================
#  ROTAS
# =========================
@app.get("/")
def dashboard():
    return send_from_directory(".", "dashboard.html")

# -------- ATIVOS (CRUD) --------
@app.get("/ativos")
def list_ativos():
    return jsonify({"ativos": db.get("ativos", []), "ativo_atual_id": db.get("ativo_atual_id")})

@app.post("/ativos")
def add_ativo():
    data = request.json or {}

    nome = str(data.get("nome", "")).strip()
    tipo = str(data.get("tipo", "lancha")).strip().lower()
    imei = str(data.get("imei", "")).strip()
    medida_base = str(data.get("medida_base", "hora")).strip().lower()
    offset = float(data.get("offset", 0.0) or 0.0)

    if not nome or not imei:
        return jsonify({"erro": "nome e imei são obrigatórios"}), 400

    if tipo not in ("lancha", "caminhao"):
        tipo = "lancha"

    if medida_base not in ("hora", "km"):
        medida_base = "hora" if tipo == "lancha" else "km"

    ativo = {
        "id": uuid.uuid4().hex[:8],
        "nome": nome,
        "tipo": tipo,
        "imei": imei,
        "medida_base": medida_base,
        "offset": offset,
        "plano_preventivo": DEFAULT_PLANO_HORAS if medida_base == "hora" else DEFAULT_PLANO_KM
    }

    db.setdefault("ativos", []).append(ativo)
    if not db.get("ativo_atual_id"):
        db["ativo_atual_id"] = ativo["id"]
    save_db(db)

    return jsonify({"mensagem": "Ativo criado", "ativo": ativo})

@app.put("/ativos/<ativo_id>")
def update_ativo(ativo_id):
    data = request.json or {}
    for a in db.get("ativos", []):
        if a["id"] == ativo_id:
            a["nome"] = str(data.get("nome", a["nome"])).strip()
            a["tipo"] = str(data.get("tipo", a["tipo"])).strip().lower()
            a["imei"] = str(data.get("imei", a["imei"])).strip()
            a["medida_base"] = str(data.get("medida_base", a["medida_base"])).strip().lower()
            a["offset"] = float(data.get("offset", a["offset"]) or 0.0)
            ensure_defaults_for_ativo(a)
            save_db(db)
            return jsonify({"mensagem": "Ativo atualizado", "ativo": a})
    return jsonify({"erro": "Ativo não encontrado"}), 404

@app.delete("/ativos/<ativo_id>")
def delete_ativo(ativo_id):
    ativos = db.get("ativos", [])
    novos = [a for a in ativos if a["id"] != ativo_id]
    if len(novos) == len(ativos):
        return jsonify({"erro": "Ativo não encontrado"}), 404

    db["ativos"] = novos
    if db.get("ativo_atual_id") == ativo_id:
        db["ativo_atual_id"] = novos[0]["id"] if novos else None
    save_db(db)
    return jsonify({"mensagem": "Ativo removido"})

@app.post("/ativos/<ativo_id>/selecionar")
def selecionar_ativo(ativo_id):
    for a in db.get("ativos", []):
        if a["id"] == ativo_id:
            db["ativo_atual_id"] = ativo_id
            save_db(db)
            return jsonify({"mensagem": "Ativo selecionado", "ativo_atual_id": ativo_id})
    return jsonify({"erro": "Ativo não encontrado"}), 404

# -------- DADOS (ativo atual) --------
@app.get("/dados")
def dados():
    ativo = get_ativo_atual()
    if not ativo:
        return jsonify({"erro": "Nenhum ativo cadastrado"}), 400

    ensure_defaults_for_ativo(ativo)
    bs = obter_dados_brasilsat_por_imei(ativo["imei"])

    horas_ajustadas = round(bs["horas_reais"] + (ativo["offset"] if ativo["medida_base"] == "hora" else 0), 2)
    km_ajustados = round(bs["km_reais"] + (ativo["offset"] if ativo["medida_base"] == "km" else 0), 2)

    if ativo["medida_base"] == "hora":
        uso_base = horas_ajustadas
        unidade_base = "h"
    else:
        uso_base = km_ajustados
        unidade_base = "km"

    return jsonify({
        "ativo_id": ativo["id"],
        "nome": ativo["nome"],
        "tipo": ativo["tipo"],
        "imei": bs["imei"],
        "motor_ligado": bs["motor_ligado"],
        "tensao_bateria": bs["tensao_bateria"],
        "servertime": bs["servertime"],

        "horas_reais_brasilsat": bs["horas_reais"],
        "km_reais_brasilsat": bs["km_reais"],

        "offset": ativo["offset"],
        "horas_motor": horas_ajustadas,
        "km_total": km_ajustados,

        "uso_base": uso_base,
        "unidade_base": unidade_base,
        "medida_base": ativo["medida_base"],
    })

# -------- PREVENTIVA (ativo atual) --------
@app.get("/preventiva")
def preventiva():
    ativo = get_ativo_atual()
    if not ativo:
        return jsonify({"erro": "Nenhum ativo cadastrado"}), 400

    ensure_defaults_for_ativo(ativo)
    bs = obter_dados_brasilsat_por_imei(ativo["imei"])

    uso_ajustado = round(
        (bs["horas_reais"] if ativo["medida_base"] == "hora" else bs["km_reais"])
        + ativo["offset"]
    , 2)

    tarefas = calcular_status_preventiva(uso_ajustado, ativo["plano_preventivo"])

    return jsonify({
        "ativo_id": ativo["id"],
        "nome": ativo["nome"],
        "tipo": ativo["tipo"],
        "imei": bs["imei"],
        "uso_ajustado": uso_ajustado,
        "unidade": "h" if ativo["medida_base"] == "hora" else "km",
        "tarefas": tarefas
    })

# -------- CONFIG OFFSET (ativo atual) --------
@app.post("/config/offset")
def set_offset():
    ativo = get_ativo_atual()
    if not ativo:
        return jsonify({"erro": "Nenhum ativo cadastrado"}), 400

    data = request.json or {}
    valor = data.get("offset")
    if valor is None:
        return jsonify({"erro": "Envie offset no corpo JSON"}), 400
    try:
        valor = float(valor)
    except:
        return jsonify({"erro": "offset deve ser número"}), 400

    ativo["offset"] = valor
    save_db(db)
    return jsonify({"mensagem": "Offset atualizado", "novo_offset": valor})

@app.get("/config/offset")
def get_offset():
    ativo = get_ativo_atual()
    if not ativo:
        return jsonify({"erro": "Nenhum ativo cadastrado"}), 400
    return jsonify({"offset": ativo["offset"], "nome": ativo["nome"], "medida_base": ativo["medida_base"]})

# -------- CONFIG PLANO (ativo atual) --------
@app.get("/config/plano")
def get_plano():
    ativo = get_ativo_atual()
    if not ativo:
        return jsonify({"erro": "Nenhum ativo cadastrado"}), 400
    ensure_defaults_for_ativo(ativo)
    return jsonify({"plano": ativo["plano_preventivo"], "medida_base": ativo["medida_base"]})

@app.post("/config/plano")
def set_plano():
    ativo = get_ativo_atual()
    if not ativo:
        return jsonify({"erro": "Nenhum ativo cadastrado"}), 400

    data = request.json or {}
    plano = data.get("plano")
    if not isinstance(plano, list):
        return jsonify({"erro": "Envie 'plano' como lista"}), 400

    novo = []
    for item in plano:
        try:
            novo.append({
                "nome": str(item["nome"]),
                "unidade": str(item.get("unidade", ativo["medida_base"])).lower(),
                "primeira_execucao": float(item["primeira_execucao"]),
                "intervalo": float(item["intervalo"]),
                "avisar_antes": float(item.get("avisar_antes", 10))
            })
        except:
            return jsonify({"erro": f"Item inválido: {item}"}), 400

    ativo["plano_preventivo"] = novo
    save_db(db)
    return jsonify({"mensagem": "Plano atualizado", "total_itens": len(novo)})

if __name__ == "__main__":
    app.run(debug=True, port=5000)
