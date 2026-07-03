"""
Hapvida — Servidor Local (FastAPI)
Roda no seu PC, expõe API para o dashboard online controlar o robô.
Inicie com: python servidor_local.py
"""

import asyncio, csv, io, json, os, subprocess, sys, threading, time
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

# ── Caminhos (ajuste se necessário) ─────────────────────────
BASE_DIR      = Path(__file__).parent
ROBO_SCRIPT   = BASE_DIR / "roboconsulta_paralelo.py"
ARQUIVO_CPF   = BASE_DIR / "cpfs.csv"
ARQUIVO_OUT   = BASE_DIR / "resultados_beneficios.csv"
ARQUIVO_REP   = BASE_DIR / "Repiques_Ativos.csv"

# ── Estado global do robô ────────────────────────────────────
estado = {
    "rodando":           False,
    "pid":               None,
    "inicio":            None,
    "workers":            12,   # quantidade solicitada pelo usuário
    "delay":              16,   # delay pós-consulta configurado
    "workers_ativos":      0,   # workers realmente logados e trabalhando
    "workers_pendentes":   0,   # workers ainda abrindo/logando
    "workers_erro":        0,   # workers que falharam ao logar
    "workers_total":       0,   # workers que o robô realmente abriu (pode ser < solicitado se houver poucos CPFs)
    "total":               0,
    "concluidos":          0,
    "repiques":            0,
    "erros":               0,
    "logs":               [],   # últimas 300 linhas
    "processo":           None,
}
estado_lock = threading.Lock()

app = FastAPI(title="Hapvida Robô API", version="1.0")

# CORS para acesso pelo dashboard do GitHub Pages
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://bolado85.github.io",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# ── Modelos ──────────────────────────────────────────────────
class ConfigRobo(BaseModel):
    workers:  int = 12
    usuario:  str = "1374"
    senha:    str = "123456"
    delay:    int = 16


# ── Helpers ─────────────────────────────────────────────────
def add_log(linha: str):
    with estado_lock:
        ts = datetime.now().strftime("%H:%M:%S")
        entrada = f"[{ts}] {linha}"
        estado["logs"].append(entrada)
        if len(estado["logs"]) > 300:
            estado["logs"] = estado["logs"][-300:]


def contar_csv(path: Path) -> int:
    if not path.exists(): return 0
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return max(0, sum(1 for _ in f) - 1)
    except: return 0


def ler_progresso_csv():
    """Atualiza contadores lendo os CSVs de saída."""
    with estado_lock:
        estado["concluidos"] = contar_csv(ARQUIVO_OUT)
        estado["repiques"]   = contar_csv(ARQUIVO_REP)


def calcular_eta():
    """Tempo estimado restante, em segundos.
    Antes de haver dados suficientes, usa a estimativa teórica
    (CPFs restantes × delay ÷ workers ativos). Depois de algumas
    consultas, passa a usar a velocidade REAL observada — por isso o
    tempo se ajusta sozinho se houver erros, travamentos ou lentidão."""
    with estado_lock:
        rodando  = estado["rodando"]
        inicio   = estado["inicio"]
        concl    = estado["concluidos"]
        delay    = estado.get("delay", 16) or 16
        w_ativos = estado.get("workers_ativos", 0)
        w_total  = estado.get("workers_total") or estado.get("workers", 1)

    if not rodando or not inicio:
        return None

    total = contar_csv(ARQUIVO_CPF)
    if total <= 0:
        return None

    restantes = max(total - concl, 0)
    if restantes == 0:
        return 0

    workers_ref = w_ativos if w_ativos > 0 else max(w_total, 1)

    try:
        elapsed = (datetime.now() - datetime.fromisoformat(inicio)).total_seconds()
    except Exception:
        elapsed = 0

    if concl >= 3 and elapsed > 5:
        taxa = concl / elapsed  # CPFs concluídos por segundo (velocidade real)
        if taxa > 0:
            return round(restantes / taxa)

    # Estimativa teórica inicial (usada assim que o robô inicia)
    return round(restantes * delay / workers_ref)


def monitorar_processo(proc: subprocess.Popen):
    """Thread que lê stdout do robô e atualiza estado."""
    import re
    pat_status = re.compile(
        r">>> STATUS \| total=(\d+) concluidos=(\d+) repiques=(\d+) erros=(\d+) "
        r"workers_ativos=(\d+) workers_pendentes=(\d+) workers_erro=(\d+) workers_total=(\d+)"
    )

    for linha in iter(proc.stdout.readline, ""):
        linha = linha.rstrip()
        if not linha: continue
        add_log(linha)

        m = pat_status.search(linha)
        if m:
            with estado_lock:
                estado["total"]             = int(m.group(1))
                estado["concluidos"]        = int(m.group(2))
                estado["repiques"]          = int(m.group(3))
                estado["erros"]             = int(m.group(4))
                estado["workers_ativos"]    = int(m.group(5))
                estado["workers_pendentes"] = int(m.group(6))
                estado["workers_erro"]      = int(m.group(7))
                estado["workers_total"]     = int(m.group(8))

    # Processo terminou
    proc.wait()
    with estado_lock:
        estado["rodando"]           = False
        estado["processo"]          = None
        estado["workers_ativos"]    = 0
        estado["workers_pendentes"] = 0
    add_log("=== Robô encerrado ===")


# ── Endpoints ────────────────────────────────────────────────

@app.get("/status")
def get_status():
    """Retorna estado atual do robô e contadores."""
    if estado["rodando"] is False:
        # só reconta pelo CSV quando o robô não está rodando (enquanto roda,
        # os contadores em tempo real vêm do STATUS emitido pelo robô)
        ler_progresso_csv()

    eta = calcular_eta()

    with estado_lock:
        return {
            "rodando":           estado["rodando"],
            "workers":           estado["workers"],
            "workers_ativos":    estado["workers_ativos"],
            "workers_pendentes": estado["workers_pendentes"],
            "workers_erro":      estado["workers_erro"],
            "workers_total":     estado["workers_total"],
            "inicio":            estado["inicio"],
            "total":             contar_csv(ARQUIVO_CPF),
            "concluidos":        estado["concluidos"],
            "repiques":          estado["repiques"],
            "erros":             estado["erros"],
            "pct":               round(estado["concluidos"] / max(contar_csv(ARQUIVO_CPF),1) * 100, 1),
            "tempo_estimado_seg": eta,
            "arquivo_cpf_existe":  ARQUIVO_CPF.exists(),
            "arquivo_out_existe":  ARQUIVO_OUT.exists(),
            "arquivo_rep_existe":  ARQUIVO_REP.exists(),
        }


@app.post("/iniciar")
def iniciar_robo(cfg: ConfigRobo):
    """Inicia o robô com a configuração enviada."""
    with estado_lock:
        if estado["rodando"]:
            raise HTTPException(400, "Robô já está rodando")

    if not ROBO_SCRIPT.exists():
        raise HTTPException(500, f"Script do robô não encontrado: {ROBO_SCRIPT}")

    # A configuração vai por linha de comando — nada de editar o .py na mão.
    # Isso evita o bug antigo de "sempre abre só 2 workers" (o patch por
    # regex não encontrava a variável no script e a configuração era ignorada).
    cmd = [
        sys.executable, "-u", str(ROBO_SCRIPT),
        "--workers", str(cfg.workers),
        "--usuario", cfg.usuario,
        "--senha",   cfg.senha,
        "--delay",   str(cfg.delay),
    ]

    # Força o processo filho a usar UTF-8 no stdout/stderr, independente
    # da code page do Windows — evita o crash de encoding com emojis
    # (ver também o reconfigure() feito dentro do próprio roboconsulta_paralelo.py)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(BASE_DIR),
        env=env,
    )

    with estado_lock:
        estado["rodando"]           = True
        estado["processo"]          = proc
        estado["workers"]           = cfg.workers
        estado["delay"]             = cfg.delay
        estado["workers_ativos"]    = 0
        estado["workers_pendentes"] = cfg.workers
        estado["workers_erro"]      = 0
        estado["workers_total"]     = cfg.workers
        estado["inicio"]            = datetime.now().isoformat()
        estado["erros"]             = 0
        estado["concluidos"]        = 0
        estado["repiques"]          = 0
        estado["logs"]              = []

    add_log(f"=== Robô iniciado com {cfg.workers} workers ===")

    t = threading.Thread(target=monitorar_processo, args=(proc,), daemon=True)
    t.start()

    return {"ok": True, "msg": f"Robô iniciado com {cfg.workers} workers"}


@app.post("/parar")
def parar_robo():
    """Para o robô (Ctrl+C seguro — salva progresso)."""
    with estado_lock:
        proc = estado.get("processo")
        if not proc:
            raise HTTPException(400, "Robô não está rodando")
        try:
            proc.terminate()
        except Exception as e:
            raise HTTPException(500, f"Erro ao parar: {e}")
        estado["rodando"]           = False
        estado["workers_ativos"]    = 0
        estado["workers_pendentes"] = 0

    add_log("=== Robô parado pelo usuário ===")
    return {"ok": True, "msg": "Robô parado. Progresso mantido."}


@app.get("/logs")
def get_logs(ultimas: int = 100):
    """Retorna as últimas N linhas de log."""
    with estado_lock:
        return {"logs": estado["logs"][-ultimas:]}


@app.get("/stream")
async def stream_logs():
    """SSE — envia atualizações em tempo real para o dashboard."""
    async def gerador():
        ultimo = 0
        while True:
            eta = calcular_eta()
            with estado_lock:
                logs   = estado["logs"]
                novos  = logs[ultimo:]
                ultimo = len(logs)
                s = {
                    "rodando":            estado["rodando"],
                    "concluidos":         estado["concluidos"],
                    "total":              contar_csv(ARQUIVO_CPF),
                    "repiques":           estado["repiques"],
                    "erros":              estado["erros"],
                    "workers_ativos":     estado["workers_ativos"],
                    "workers_pendentes":  estado["workers_pendentes"],
                    "workers_erro":       estado["workers_erro"],
                    "workers_total":      estado["workers_total"],
                    "tempo_estimado_seg": eta,
                    "logs":               novos,
                }
            yield f"data: {json.dumps(s, ensure_ascii=False)}\n\n"
            await asyncio.sleep(1.5)

    return StreamingResponse(gerador(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache",
                 "X-Accel-Buffering": "no"})


# Cabeçalhos que proíbem QUALQUER cache (navegador, proxy, ngrok) dos
# downloads. Sem isso, o FileResponse manda um "Last-Modified" e o
# navegador acha que pode reaproveitar o CSV baixado anteriormente —
# por isso, depois de "Limpar Tudo" + nova consulta, o download às
# vezes ainda trazia os dados da 1ª consulta (vinha do cache do
# navegador, não do arquivo novo no disco).
HEADERS_SEM_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/download/completo")
def download_completo():
    if not ARQUIVO_OUT.exists():
        raise HTTPException(404, "Arquivo não gerado ainda")
    return FileResponse(str(ARQUIVO_OUT),
        media_type="text/csv",
        filename="resultados_beneficios.csv",
        headers=HEADERS_SEM_CACHE)


@app.get("/download/repiques")
def download_repiques():
    if not ARQUIVO_REP.exists():
        raise HTTPException(404, "Arquivo de repiques não gerado ainda")
    return FileResponse(str(ARQUIVO_REP),
        media_type="text/csv",
        filename="Repiques_Ativos.csv",
        headers=HEADERS_SEM_CACHE)


@app.get("/dados/repiques")
def dados_repiques(limit: int = 500):
    """Retorna os repiques ativos como JSON para a tabela do dashboard."""
    if not ARQUIVO_REP.exists():
        return {"dados": [], "total": 0}
    rows = []
    try:
        with open(ARQUIVO_REP, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                rows.append(dict(row))
    except: pass
    return {"dados": rows[:limit], "total": len(rows)}


@app.get("/dados/todos")
def dados_todos(limit: int = 200, busca: str = ""):
    """Retorna todos os resultados como JSON com filtro de busca."""
    if not ARQUIVO_OUT.exists():
        return {"dados": [], "total": 0}
    rows = []
    busca_l = busca.lower()
    try:
        with open(ARQUIVO_OUT, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                r = dict(row)
                if not busca or any(busca_l in str(v).lower() for v in r.values()):
                    rows.append(r)
    except: pass
    return {"dados": rows[:limit], "total": len(rows)}


@app.post("/limpar")
def limpar_dados():
    """
    Apaga a lista de CPFs e os resultados (completo + repiques) para
    começar uma nova análise do zero. Recusa se o robô estiver rodando,
    para não apagar um trabalho em andamento.
    """
    with estado_lock:
        if estado["rodando"]:
            raise HTTPException(400, "Pare o robô antes de limpar os dados.")

    removidos = []
    erros = []
    for arq in (ARQUIVO_CPF, ARQUIVO_OUT, ARQUIVO_REP):
        try:
            if arq.exists():
                arq.unlink()
                removidos.append(arq.name)
        except Exception as e:
            erros.append(f"{arq.name}: {e}")

    with estado_lock:
        estado.update({
            "inicio": None, "total": 0, "concluidos": 0, "repiques": 0,
            "erros": 0, "workers_ativos": 0, "workers_pendentes": 0,
            "workers_erro": 0, "workers_total": 0, "logs": [],
        })

    add_log("=== Dados limpos — pronto para uma nova análise ===")

    if erros:
        raise HTTPException(500, f"Removido parcialmente. Erros: {'; '.join(erros)}")
    return {"ok": True, "removidos": removidos}


@app.post("/upload/cpfs")
async def upload_cpfs(request: Request):
    """Recebe a lista de CPFs enviada pelo dashboard (um por linha) e substitui cpfs.csv."""
    body = await request.body()
    try:
        with open(ARQUIVO_CPF, "wb") as f:
            f.write(body)
        total = contar_csv(ARQUIVO_CPF)
        add_log(f"=== {total} CPF(s) recebidos do dashboard ===")
        return {"ok": True, "total": total}
    except Exception as e:
        raise HTTPException(500, str(e))


if __name__ == "__main__":
    print()
    print("  ╔══════════════════════════════════════════════╗")
    print("  ║   HAPVIDA — Servidor Local                   ║")
    print("  ║   API rodando em http://localhost:8000       ║")
    print("  ║   Deixe esta janela aberta                   ║")
    print("  ╚══════════════════════════════════════════════╝")
    print()
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
