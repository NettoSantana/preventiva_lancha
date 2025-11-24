from flask import Flask, jsonify, send_from_directory, request
import time, hashlib, requests, os, math

app = Flask(__name__)

# =========================
#  BRASILSAT (API)
# =========================
BASE_URL = "https://gps.brasilsatgps.com.br"
ACCOUNT  = os.getenv("BRASILSAT_ACCOUNT", "nettosantana@icloud.com")
PASSWORD = os.getenv("BRASILSAT_PASSWORD", "1234567")
IMEI     = os.getenv("BRASILSAT_IMEI", "355468593059041")

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
#  EMBARCAÇÃO (CONFIG)
# =========================
embarcacao = {
    "nome": "Lancha Cliente 01",
    "imei": IMEI,
    "offset_horas": 0.0
}

# =========================
#  PLANO PREVENTIVO (editável pelo dashboard)
# =========================
plano_preventivo = [
    {
        "nome": "Troca de óleo do motor",
        "primeira_execucao_h": 100,
        "intervalo_h": 100,
        "avisar_antes_h": 10
    },
    {
        "nome": "Troca do filtro de óleo",
        "primeira_execucao_h": 100,
        "intervalo_h": 100,
        "avisar_antes_h": 10
    },
    {
        "nome": "Drenar separador de água/combustível",
        "primeira_execucao_h": 100,
        "intervalo_h": 100,
        "avisar_antes_h": 10
    },
    {
        "nome": "Troca do filtro de combustível",
        "primeira_execucao_h": 200,
        "intervalo_h": 200,
        "avisar_antes_h": 10
    }
]

def calcular_status_preventiva(horas_ajustadas: float):
    tarefas = []
    for item in plano_preventivo:
        primeira = float(item["primeira_execucao_h"])
        intervalo = float(item["intervalo_h"])
        avisar_antes = float(item.get("avisar_antes_h", 10))

        if horas_ajustadas < primeira:
            proxima = primeira
        else:
            ciclos = math.floor((horas_ajustadas - primeira) / intervalo)
            proxima = primeira + (ciclos + 1) * intervalo

        faltam = round(proxima - horas_ajustadas, 2)

        if faltam <= 0:
            status = "ATRASADO"
        elif faltam <= avisar_antes:
            status = "ATENCAO"
        else:
            status = "OK"

        tarefas.append({
            "nome": item["nome"],
            "primeira_execucao_h": primeira,
            "intervalo_h": intervalo,
            "avisar_antes_h": avisar_antes,
            "proxima_execucao_h": round(proxima, 2),
            "faltam_h": faltam,
            "status": status
        })

    prioridade = {"ATRASADO": 0, "ATENCAO": 1, "OK": 2}
    tarefas.sort(key=lambda x: (prioridade[x["status"]], x["faltam_h"]))
    return tarefas

def obter_dados_brasilsat():
    token = get_token()
    data = track(token, IMEI)

    acctime_s = int(data.get("acctime") or 0)
    horas_reais = round(acctime_s / 3600.0, 2)

    return {
        "imei": data.get("imei"),
        "motor_ligado": bool(int(data.get("accstatus") or 0)),
        "horas_reais": horas_reais,
        "tensao_bateria": float(data.get("externalpower") or 0),
        "servertime": data.get("servertime")
    }

# =========================
#  ROTAS
# =========================

@app.get("/")
def dashboard():
    return send_from_directory(".", "dashboard.html")

@app.get("/dados")
def dados():
    bs = obter_dados_brasilsat()
    horas_ajustadas = round(bs["horas_reais"] + embarcacao["offset_horas"], 2)

    return jsonify({
        "lancha": embarcacao["nome"],
        "imei": bs["imei"],
        "motor_ligado": bs["motor_ligado"],
        "horas_motor": horas_ajustadas,
        "tensao_bateria": bs["tensao_bateria"],
        "servertime": bs["servertime"],
        "offset_horas": embarcacao["offset_horas"],
        "horas_reais_brasilsat": bs["horas_reais"]
    })

@app.get("/preventiva")
def preventiva():
    bs = obter_dados_brasilsat()
    horas_ajustadas = round(bs["horas_reais"] + embarcacao["offset_horas"], 2)

    return jsonify({
        "lancha": embarcacao["nome"],
        "imei": bs["imei"],
        "horas_ajustadas": horas_ajustadas,
        "tarefas": calcular_status_preventiva(horas_ajustadas)
    })

# =========================
#  CONFIG OFFSET (já tinha)
# =========================
@app.post("/config/offset")
def set_offset():
    data = request.json or {}
    valor = data.get("offset_horas")
    if valor is None:
        return jsonify({"erro": "Envie offset_horas no corpo JSON"}), 400
    try:
        valor = float(valor)
    except:
        return jsonify({"erro": "offset_horas deve ser número"}), 400

    embarcacao["offset_horas"] = valor
    return jsonify({"mensagem": "Offset atualizado", "novo_offset": valor})

@app.get("/config/offset")
def get_offset():
    return jsonify({"offset_horas": embarcacao["offset_horas"], "lancha": embarcacao["nome"]})

# =========================
#  NOVO — CONFIG PLANO PREVENTIVO
# =========================
@app.get("/config/plano")
def get_plano():
    return jsonify({"plano": plano_preventivo})

@app.post("/config/plano")
def set_plano():
    data = request.json or {}
    plano = data.get("plano")
    if not isinstance(plano, list):
        return jsonify({"erro": "Envie 'plano' como lista"}), 400

    # validação simples
    novo = []
    for item in plano:
        try:
            novo.append({
                "nome": str(item["nome"]),
                "primeira_execucao_h": float(item["primeira_execucao_h"]),
                "intervalo_h": float(item["intervalo_h"]),
                "avisar_antes_h": float(item.get("avisar_antes_h", 10))
            })
        except Exception as e:
            return jsonify({"erro": f"Item inválido: {item}"}), 400

    plano_preventivo.clear()
    plano_preventivo.extend(novo)

    return jsonify({"mensagem": "Plano atualizado", "total_itens": len(plano_preventivo)})

if __name__ == "__main__":
    app.run(debug=True, port=5000)
