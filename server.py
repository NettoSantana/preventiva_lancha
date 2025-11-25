from flask import Flask, jsonify, send_from_directory, request
import time
import hashlib
import requests
import os
import math
import json
import uuid

app = Flask(__name__)

# =========================
#  BRASILSAT (API)
# =========================
BASE_URL = "https://gps.brasilsatgps.com.br"
ACCOUNT = os.getenv("BRASILSAT_ACCOUNT", "nettosantana@icloud.com")
PASSWORD = os.getenv("BRASILSAT_PASSWORD", "1234567")

# --- CACHE DE TOKEN (evitar /authorization toda hora) ---
TOKEN_CACHE = {
    "token": None,
    "expires_at": 0,  # epoch em segundos
}

# --- CACHE DE TRACK POR IMEI (evitar bater na BrasilSat toda hora) ---
BRASILSAT_CACHE = {}
BRASILSAT_TTL = 20  # segundos (atualização a cada 20s)


def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def get_token():
    """
    Obtém token da BrasilSat com cache.
    Só chama /api/authorization quando o token expirar.
    """
    now = int(time.time())

    # Se ainda está válido, reaproveita
    if TOKEN_CACHE["token"] and now < TOKEN_CACHE["expires_at"]:
        return TOKEN_CACHE["token"]

    signature = md5(md5(PASSWORD) + str(now))
    url = f"{BASE_URL}/api/authorization"
    r = requests.get(
        url,
        params={"time": now, "account": ACCOUNT, "signature": signature},
        timeout=15,
    )
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 0:
        raise RuntimeError(f"Auth falhou: {j}")

    token = j["record"]["access_token"]

    # Chuto validade de 50 minutos pra não ficar sem renovar
    TOKEN_CACHE["token"] = token
    TOKEN_CACHE["expires_at"] = now + 50 * 60

    return token


def track(access_token: str, imei: str):
    url = f"{BASE_URL}/api/track"
    r = requests.get(
        url, params={"access_token": access_token, "imeis": imei}, timeout=15
    )
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 0:
        raise RuntimeError(f"Track falhou: {j}")
    return j["record"][0]


# =========================
#  "DB" SIMPLES EM JSON (ATIVOS)
# =========================
DB_FILE = os.path.join(os.path.dirname(__file__), "db.json")

# "DB" de clientes em arquivo separado
CLIENTES_FILE = os.path.join(os.path.dirname(__file__), "clientes.json")

DEFAULT_PLANO_HORAS = [
    {
        "nome": "Troca de óleo do motor",
        "unidade": "hora",
        "primeira_execucao": 100,
        "intervalo": 100,
        "avisar_antes": 10,
    },
    {
        "nome": "Troca do filtro de óleo",
        "unidade": "hora",
        "primeira_execucao": 100,
        "intervalo": 100,
        "avisar_antes": 10,
    },
    {
        "nome": "Drenar separador de água/combustível",
        "unidade": "hora",
        "primeira_execucao": 100,
        "intervalo": 100,
        "avisar_antes": 10,
    },
    {
        "nome": "Troca do filtro de combustível",
        "unidade": "hora",
        "primeira_execucao": 200,
        "intervalo": 200,
        "avisar_antes": 10,
    },
]

DEFAULT_PLANO_KM = [
    {
        "nome": "Troca de óleo do motor",
        "unidade": "km",
        "primeira_execucao": 10000,
        "intervalo": 10000,
        "avisar_antes": 500,
    },
    {
        "nome": "Troca do filtro de óleo",
        "unidade": "km",
        "primeira_execucao": 10000,
        "intervalo": 10000,
        "avisar_antes": 500,
    },
    {
        "nome": "Troca do filtro de combustível",
        "unidade": "km",
        "primeira_execucao": 20000,
        "intervalo": 20000,
        "avisar_antes": 1000,
    },
]

# ===== ATIVO DEFAULT (bootstrap) =====
BOOTSTRAP_IMEI = os.getenv("BRASILSAT_IMEI", "355468593059041")
BOOTSTRAP_NOME = os.getenv(
    "BOOTSTRAP_NOME", "Electro Auto Náutica — Embarcação 01"
)
BOOTSTRAP_TIPO = os.getenv("BOOTSTRAP_TIPO", "lancha")  # lancha | caminhao
BOOTSTRAP_MEDIDA = os.getenv("BOOTSTRAP_MEDIDA", "hora")  # hora | km


def load_db():
    if not os.path.exists(DB_FILE):
        return {"ativos": [], "ativo_atual_id": None}
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"ativos": [], "ativo_atual_id": None}


def save_db(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def load_clientes():
    if not os.path.exists(CLIENTES_FILE):
        return {"clientes": []}
    try:
        with open(CLIENTES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"clientes": []}


def save_clientes(data):
    with open(CLIENTES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


db = load_db()
clientes_db = load_clientes()


def ensure_defaults_for_ativo(ativo):
    # plano preventivo default
    if "plano_preventivo" not in ativo or not isinstance(
        ativo.get("plano_preventivo"), list
    ):
        ativo["plano_preventivo"] = (
            DEFAULT_PLANO_HORAS
            if ativo.get("medida_base") == "hora"
            else DEFAULT_PLANO_KM
        )

    # base cumulativa antiga (km) – mantida para compatibilidade
    ativo.setdefault("horas_base_total", 0.0)
    ativo.setdefault("km_base_total", 0.0)

    # estado de horas paradas
    ativo.setdefault(
        "paradas_state", {"last_ts": 0, "last_motor": False, "acc_s": 0}
    )

    # NOVO: horímetro cumulativo interno
    ativo.setdefault("horas_totais", 0.0)
    ativo.setdefault(
        "horimetro_state", {"last_ts": 0, "last_motor": False}
    )


def bootstrap_db_if_needed():
    """Se subir com db.json vazio, cria um ativo default automaticamente."""
    ativos = db.get("ativos", [])
    if ativos:
        for a in ativos:
            ensure_defaults_for_ativo(a)
        if not db.get("ativo_atual_id") and ativos:
            db["ativo_atual_id"] = ativos[0]["id"]
        save_db(db)
        return

    medida = (BOOTSTRAP_MEDIDA or "").lower()
    tipo = (BOOTSTRAP_TIPO or "").lower()

    if tipo not in ("lancha", "caminhao"):
        tipo = "lancha"
    if medida not in ("hora", "km"):
        medida = "hora" if tipo == "lancha" else "km"

    ativo = {
        "id": uuid.uuid4().hex[:8],
        "nome": BOOTSTRAP_NOME,
        "tipo": tipo,
        "imei": BOOTSTRAP_IMEI,
        "medida_base": medida,
        "offset": 0.0,
        "plano_preventivo": (
            DEFAULT_PLANO_HORAS if medida == "hora" else DEFAULT_PLANO_KM
        ),
        "horas_base_total": 0.0,
        "km_base_total": 0.0,
        "paradas_state": {"last_ts": 0, "last_motor": False, "acc_s": 0},
        "horas_totais": 0.0,
        "horimetro_state": {"last_ts": 0, "last_motor": False},
    }

    db["ativos"] = [ativo]
    db["ativo_atual_id"] = ativo["id"]
    save_db(db)


bootstrap_db_if_needed()


def get_ativo_atual():
    ativos = db.get("ativos", [])
    if not ativos:
        return None

    ativo_id = db.get("ativo_atual_id")
    if ativo_id:
        for a in ativos:
            if a["id"] == ativo_id:
                return a

    db["ativo_atual_id"] = ativos[0]["id"]
    save_db(db)
    return ativos[0]


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

        tarefas.append(
            {
                "nome": item.get("nome", ""),
                "unidade": unidade,
                "primeira_execucao": primeira,
                "intervalo": intervalo,
                "avisar_antes": avisar_antes,
                "proxima_execucao": round(proxima, 2),
                "faltam": faltam,
                "status": status,
            }
        )

    prioridade = {"ATRASADO": 0, "ATENCAO": 1, "OK": 2}
    tarefas.sort(key=lambda x: (prioridade[x["status"]], x["faltam"]))
    return tarefas


def obter_dados_brasilsat_por_imei(imei: str):
    """
    Busca dados da BrasilSat, com cache de 20s por IMEI.
    Isso reduz chamadas e evita Over TPS Limit.
    """
    now = time.time()
    cached = BRASILSAT_CACHE.get(imei)
    if cached and (now - cached["last_fetch"] < BRASILSAT_TTL):
        return cached["data"]

    # Busca "real" na BrasilSat
    token = get_token()
    data = track(token, imei)

    acctime_s = int(data.get("acctime") or 0)
    horas_reais = round(acctime_s / 3600.0, 2)

    mileage_m = float(data.get("mileage") or 0)
    km_total = round(mileage_m / 1000.0, 2)

    result = {
        "imei": data.get("imei"),
        "motor_ligado": bool(int(data.get("accstatus") or 0)),
        "horas_reais": horas_reais,      # tempo do ciclo atual (BrasilSat)
        "km_reais": km_total,
        "tensao_bateria": float(data.get("externalpower") or 0),
        "servertime": int(data.get("servertime") or time.time()),
    }

    BRASILSAT_CACHE[imei] = {
        "last_fetch": now,
        "data": result,
    }
    return result


# =========================
#  HORAS PARADAS (zera ao ligar)
# =========================
def atualizar_horas_paradas(ativo, servertime: int, motor_ligado: bool):
    st = ativo.get("paradas_state") or {}
    last_ts = int(st.get("last_ts") or 0)
    last_motor = bool(st.get("last_motor") or False)
    acc_s = int(st.get("acc_s") or 0)

    if last_ts == 0:
        last_ts = servertime
        last_motor = motor_ligado
        acc_s = 0

    if (not last_motor) and (not motor_ligado):
        delta = max(0, servertime - last_ts)
        acc_s += delta

    if (not last_motor) and motor_ligado:
        # ligou o motor -> zera horas paradas
        acc_s = 0

    st["last_ts"] = servertime
    st["last_motor"] = motor_ligado
    st["acc_s"] = acc_s
    ativo["paradas_state"] = st

    return round(acc_s / 3600.0, 2)


# =========================
#  HORÍMETRO CUMULATIVO
# =========================
def atualizar_horas_totais(ativo, servertime: int, motor_ligado: bool):
    """
    Acumula horas de motor ligado usando servertime.
    Não depende do acctime (que zera a cada ciclo).
    """
    st = ativo.get("horimetro_state") or {}
    last_ts = int(st.get("last_ts") or 0)
    last_motor = bool(st.get("last_motor") or False)
    total = float(ativo.get("horas_totais") or 0.0)

    if last_ts == 0:
        # primeira leitura, só inicializa
        st["last_ts"] = servertime
        st["last_motor"] = motor_ligado
        ativo["horimetro_state"] = st
        ativo["horas_totais"] = total
        return round(total, 2)

    delta = max(0, servertime - last_ts)
    if last_motor:
        # estava ligado nesse intervalo => soma ao horímetro
        total += delta / 3600.0

    st["last_ts"] = servertime
    st["last_motor"] = motor_ligado
    ativo["horimetro_state"] = st
    ativo["horas_totais"] = total

    return round(total, 2)


# =========================
#  ROTAS BÁSICAS
# =========================
@app.get("/")
def dashboard():
    return send_from_directory(".", "dashboard.html")


# NOVA ROTA: tela de cadastro de cliente
@app.get("/cadastro")
def cadastro():
    # cadastro.html precisa estar na mesma pasta de server.py
    return send_from_directory(".", "cadastro.html")


# NOVAS ROTAS: painel geral (visão por cliente)
@app.get("/painel_geral")
def painel_geral():
    return send_from_directory(".", "painel_geral.html")


@app.get("/painel_geral.html")
def painel_geral_html():
    return send_from_directory(".", "painel_geral.html")


# ROTA PARA SERVIR A LOGO
@app.get("/logo.jpeg")
def logo():
    # logo.jpeg deve estar no mesmo diretório que server.py e dashboards
    return send_from_directory(".", "logo.jpeg")


# -------- ATIVOS (CRUD) --------
@app.get("/ativos")
def list_ativos():
    return jsonify(
        {"ativos": db.get("ativos", []), "ativo_atual_id": db.get("ativo_atual_id")}
    )


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
        "plano_preventivo": (
            DEFAULT_PLANO_HORAS
            if medida_base == "hora"
            else DEFAULT_PLANO_KM
        ),
        "horas_base_total": 0.0,
        "km_base_total": 0.0,
        "paradas_state": {"last_ts": 0, "last_motor": False, "acc_s": 0},
        "horas_totais": 0.0,
        "horimetro_state": {"last_ts": 0, "last_motor": False},
    }

    db.setdefault("ativos", []).append(ativo)
    if not db.get("ativo_atual_id"):
        db["ativo_atual_id"] = ativo["id"]
    ensure_defaults_for_ativo(ativo)
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
            a["medida_base"] = str(
                data.get("medida_base", a["medida_base"])
            ).strip().lower()
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
            return jsonify(
                {"mensagem": "Ativo selecionado", "ativo_atual_id": ativo_id}
            )
    return jsonify({"erro": "Ativo não encontrado"}), 404


# -------- DADOS (ativo atual) --------
@app.get("/dados")
def dados():
    ativo = get_ativo_atual()
    if not ativo:
        bootstrap_db_if_needed()
        ativo = get_ativo_atual()
        if not ativo:
            return jsonify({"erro": "Nenhum ativo cadastrado"}), 400

    ensure_defaults_for_ativo(ativo)
    bs = obter_dados_brasilsat_por_imei(ativo["imei"])

    # Leitura da BrasilSat
    horas_reais_brasilsat = bs["horas_reais"]
    km_reais_brasilsat = bs["km_reais"]

    # Offset por tipo de medida
    if ativo["medida_base"] == "hora":
        horas_paradas = atualizar_horas_paradas(
            ativo, bs["servertime"], bs["motor_ligado"]
        )
        horas_totais = atualizar_horas_totais(
            ativo, bs["servertime"], bs["motor_ligado"]
        )

        uso_base = horas_totais          # "hora da embarcação" (cumulativa)
        unidade_base = "h"

        payload = {
            "ativo_id": ativo["id"],
            "nome": ativo["nome"],
            "tipo": ativo["tipo"],
            "imei": bs["imei"],
            "motor_ligado": bs["motor_ligado"],
            "tensao_bateria": bs["tensao_bateria"],
            "servertime": bs["servertime"],
            "horas_reais_brasilsat": horas_reais_brasilsat,
            "km_reais_brasilsat": km_reais_brasilsat,
            "offset": ativo["offset"],
            "horas_motor": horas_reais_brasilsat,   # ciclo atual (referência)
            "uso_base": uso_base,
            "unidade_base": unidade_base,
            "medida_base": ativo["medida_base"],
            "horas_paradas": horas_paradas,
            "horas_totais": horas_totais,
        }

    else:
        # medida_base = "km" (usa BrasilSat como odômetro)
        km_ajustados = round(
            km_reais_brasilsat + (ativo["offset"] or 0.0), 2
        )
        km_totais = round(
            float(ativo.get("km_base_total", 0.0)) + km_ajustados, 2
        )

        horas_paradas = atualizar_horas_paradas(
            ativo, bs["servertime"], bs["motor_ligado"]
        )

        uso_base = km_totais
        unidade_base = "km"

        payload = {
            "ativo_id": ativo["id"],
            "nome": ativo["nome"],
            "tipo": ativo["tipo"],
            "imei": bs["imei"],
            "motor_ligado": bs["motor_ligado"],
            "tensao_bateria": bs["tensao_bateria"],
            "servertime": bs["servertime"],
            "horas_reais_brasilsat": horas_reais_brasilsat,
            "km_reais_brasilsat": km_reais_brasilsat,
            "offset": ativo["offset"],
            "km_total": km_ajustados,
            "uso_base": uso_base,
            "unidade_base": unidade_base,
            "medida_base": ativo["medida_base"],
            "horas_paradas": horas_paradas,
            "km_totais": km_totais,
            "km_base_total": float(ativo.get("km_base_total", 0.0)),
        }

    save_db(db)
    return jsonify(payload)


# -------- PREVENTIVA (ativo atual) --------
@app.get("/preventiva")
def preventiva():
    ativo = get_ativo_atual()
    if not ativo:
        bootstrap_db_if_needed()
        ativo = get_ativo_atual()
        if not ativo:
            return jsonify({"erro": "Nenhum ativo cadastrado"}), 400

    ensure_defaults_for_ativo(ativo)
    bs = obter_dados_brasilsat_por_imei(ativo["imei"])

    if ativo["medida_base"] == "hora":
        # usa horímetro cumulativo interno
        uso_ajustado = atualizar_horas_totais(
            ativo, bs["servertime"], bs["motor_ligado"]
        )
        unidade = "h"
    else:
        # usa odômetro da BrasilSat + offset
        uso_ajustado = round(bs["km_reais"] + (ativo["offset"] or 0.0), 2)
        unidade = "km"

    tarefas = calcular_status_preventiva(uso_ajustado, ativo["plano_preventivo"])
    save_db(db)

    return jsonify(
        {
            "ativo_id": ativo["id"],
            "nome": ativo["nome"],
            "tipo": ativo["tipo"],
            "imei": bs["imei"],
            "uso_ajustado": uso_ajustado,
            "unidade": unidade,
            "tarefas": tarefas,
        }
    )


# -------- CONFIG OFFSET (ativo atual) --------
@app.post("/config/offset")
def set_offset():
    ativo = get_ativo_atual()
    if not ativo:
        bootstrap_db_if_needed()
        ativo = get_ativo_atual()
        if not ativo:
            return jsonify({"erro": "Nenhum ativo cadastrado"}), 400

    data = request.json or {}
    valor = data.get("offset")
    if valor is None:
        return jsonify({"erro": "Envie offset no corpo JSON"}), 400
    try:
        valor = float(valor)
    except Exception:
        return jsonify({"erro": "offset deve ser número"}), 400

    ativo["offset"] = valor
    save_db(db)
    return jsonify({"mensagem": "Offset atualizado", "novo_offset": valor})


@app.get("/config/offset")
def get_offset():
    ativo = get_ativo_atual()
    if not ativo:
        bootstrap_db_if_needed()
        ativo = get_ativo_atual()
        if not ativo:
            return jsonify({"erro": "Nenhum ativo cadastrado"}), 400
    return jsonify(
        {
            "offset": ativo["offset"],
            "nome": ativo["nome"],
            "medida_base": ativo["medida_base"],
        }
    )


# -------- CONFIG PLANO (ativo atual) --------
@app.get("/config/plano")
def get_plano():
    ativo = get_ativo_atual()
    if not ativo:
        bootstrap_db_if_needed()
        ativo = get_ativo_atual()
        if not ativo:
            return jsonify({"erro": "Nenhum ativo cadastrado"}), 400
    ensure_defaults_for_ativo(ativo)
    return jsonify(
        {"plano": ativo["plano_preventivo"], "medida_base": ativo["medida_base"]}
    )


@app.post("/config/plano")
def set_plano():
    ativo = get_ativo_atual()
    if not ativo:
        bootstrap_db_if_needed()
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
            novo.append(
                {
                    "nome": str(item["nome"]),
                    "unidade": str(
                        item.get("unidade", ativo["medida_base"])
                    ).lower(),
                    "primeira_execucao": float(item["primeira_execucao"]),
                    "intervalo": float(item["intervalo"]),
                    "avisar_antes": float(item.get("avisar_antes", 10)),
                }
            )
        except Exception:
            return jsonify({"erro": f"Item inválido: {item}"}), 400

    ativo["plano_preventivo"] = novo
    save_db(db)
    return jsonify({"mensagem": "Plano atualizado", "total_itens": len(novo)})


# -------- CONFIG HORAS TOTAIS (ativo hora) --------
@app.get("/config/horas_totais")
def get_horas_totais():
    ativo = get_ativo_atual()
    if not ativo:
        bootstrap_db_if_needed()
        ativo = get_ativo_atual()
        if not ativo:
            return jsonify({"erro": "Nenhum ativo cadastrado"}), 400
    ensure_defaults_for_ativo(ativo)

    if ativo["medida_base"] != "hora":
        return jsonify({"erro": "Ativo atual não é por horas"}), 400

    bs = obter_dados_brasilsat_por_imei(ativo["imei"])
    horas_totais = atualizar_horas_totais(
        ativo, bs["servertime"], bs["motor_ligado"]
    )
    save_db(db)

    return jsonify(
        {
            "horas_totais": horas_totais,
            "horas_motor_atual": bs["horas_reais"],  # ciclo da BrasilSat
        }
    )


@app.post("/config/horas_totais")
def set_horas_totais():
    """
    Botão de ajuste do horímetro:
    você informa o total desejado, e a partir daí ele continua somando.
    """
    ativo = get_ativo_atual()
    if not ativo:
        bootstrap_db_if_needed()
        ativo = get_ativo_atual()
        if not ativo:
            return jsonify({"erro": "Nenhum ativo cadastrado"}), 400
    ensure_defaults_for_ativo(ativo)

    if ativo["medida_base"] != "hora":
        return jsonify({"erro": "Ativo atual não é por horas"}), 400

    data = request.json or {}
    horas_totais = data.get("horas_totais")
    if horas_totais is None:
        return jsonify({"erro": "Envie horas_totais no corpo JSON"}), 400
    try:
        horas_totais = float(horas_totais)
    except Exception:
        return jsonify({"erro": "horas_totais deve ser número"}), 400

    ativo["horas_totais"] = horas_totais
    # reseta o estado do horímetro pra evitar somas erradas
    ativo["horimetro_state"] = {"last_ts": 0, "last_motor": False}
    save_db(db)

    return jsonify(
        {
            "mensagem": "Horas totais ajustadas",
            "horas_totais": horas_totais,
        }
    )


# -------- CONFIG KM TOTAIS (ativo km) --------
@app.get("/config/km_totais")
def get_km_totais():
    ativo = get_ativo_atual()
    if not ativo:
        bootstrap_db_if_needed()
        ativo = get_ativo_atual()
        if not ativo:
            return jsonify({"erro": "Nenhum ativo cadastrado"}), 400
    ensure_defaults_for_ativo(ativo)

    if ativo["medida_base"] != "km":
        return jsonify({"erro": "Ativo atual não é por km"}), 400

    bs = obter_dados_brasilsat_por_imei(ativo["imei"])
    km_ajustados = round(bs["km_reais"] + (ativo["offset"] or 0.0), 2)
    km_totais = round(
        float(ativo.get("km_base_total", 0.0)) + km_ajustados, 2
    )

    return jsonify(
        {
            "km_totais": km_totais,
            "km_base_total": float(ativo.get("km_base_total", 0.0)),
            "km_total_atual": km_ajustados,
        }
    )


@app.post("/config/km_totais")
def set_km_totais():
    ativo = get_ativo_atual()
    if not ativo:
        bootstrap_db_if_needed()
        ativo = get_ativo_atual()
        if not ativo:
            return jsonify({"erro": "Nenhum ativo cadastrado"}), 400
    ensure_defaults_for_ativo(ativo)

    if ativo["medida_base"] != "km":
        return jsonify({"erro": "Ativo atual não é por km"}), 400

    data = request.json or {}
    km_totais = data.get("km_totais")
    if km_totais is None:
        return jsonify({"erro": "Envie km_totais no corpo JSON"}), 400
    try:
        km_totais = float(km_totais)
    except Exception:
        return jsonify({"erro": "km_totais deve ser número"}), 400

    bs = obter_dados_brasilsat_por_imei(ativo["imei"])
    km_ajustados = round(bs["km_reais"] + (ativo["offset"] or 0.0), 2)

    ativo["km_base_total"] = round(km_totais - km_ajustados, 2)
    save_db(db)

    return jsonify(
        {
            "mensagem": "KM totais atualizados",
            "km_totais": km_totais,
            "km_base_total": float(ativo["km_base_total"]),
        }
    )


# -------- CLIENTES (CADASTRO SIMPLES) --------
@app.post("/api/clientes")
def create_cliente():
    """
    Recebe os dados do formulário de cadastro.html e grava em clientes.json.
    Campos obrigatórios: nome_proprietario e imei_motor (pra não salvar cadastro vazio).
    """
    data = request.json or {}

    nome_prop = str(data.get("nome_proprietario", "")).strip()
    imei_motor = str(data.get("imei_motor", "")).strip()

    if not nome_prop or not imei_motor:
        return jsonify(
            {"erro": "nome_proprietario e imei_motor são obrigatórios"}
        ), 400

    cliente = {
        "id": uuid.uuid4().hex[:8],
        "created_at": int(time.time()),
        **data,
    }

    clientes_db.setdefault("clientes", []).append(cliente)
    save_clientes(clientes_db)

    return jsonify(
        {
            "mensagem": "Cliente cadastrado com sucesso",
            "cliente_id": cliente["id"],
        }
    )


@app.get("/api/clientes")
def list_clientes():
    """
    Lista simples de clientes para o painel_geral consumir.
    """
    return jsonify(clientes_db.get("clientes", []))


if __name__ == "__main__":
    app.run(debug=True, port=5000)