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
from fastapi import FastAPI, HTTPException, BackgroundTasks
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
    "rodando":     False,
    "pid":         None,
    "inicio":      None,
    "workers":     12,
    "total":       0,
    "concluidos":  0,
    "repiques":    0,
    "erros":       0,
    "logs":        [],   # últimas 200 linhas
    "processo":    None,
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


def monitorar_processo(proc: subprocess.Popen):
    """Thread que lê stdout do robô e atualiza estado."""
    import re
    pat_rep   = re.compile(r"Repiques:(\d+)")
    pat_cpfs  = re.compile(r"\((\d+)/(\d+)\)")
    pat_erros = re.compile(r"Erros\s*:\s*(\d+)")

    for linha in iter(proc.stdout.readline, ""):
        linha = linha.rstrip()
        if not linha: continue
        add_log(linha)

        # Extrair progresso da barra
        m = pat_cpfs.search(linha)
        if m:
            with estado_lock:
                estado["concluidos"] = int(m.group(1))
                estado["total"]      = int(m.group(2))

        m = pat_rep.search(linha)
        if m:
            with estado_lock:
                estado["repiques"] = int(m.group(1))

        m = pat_erros.search(linha)
        if m:
            with estado_lock:
                estado["erros"] = int(m.group(1))

    # Processo terminou
    proc.wait()
    with estado_lock:
        estado["rodando"]  = False
        estado["processo"] = None
    add_log("=== Robô encerrado ===")


# ── Endpoints ────────────────────────────────────────────────

@app.get("/status")
def get_status():
    """Retorna estado atual do robô e contadores."""
    ler_progresso_csv()
    total_cpfs = contar_csv(ARQUIVO_CPF) + \
                 (contar_csv(ARQUIVO_OUT) if ARQUIVO_OUT.exists() else 0)

    with estado_lock:
        return {
            "rodando":    estado["rodando"],
            "workers":    estado["workers"],
            "inicio":     estado["inicio"],
            "total":      contar_csv(ARQUIVO_CPF),
            "concluidos": estado["concluidos"],
            "repiques":   estado["repiques"],
            "erros":      estado["erros"],
            "pct":        round(estado["concluidos"] / max(contar_csv(ARQUIVO_CPF),1) * 100, 1),
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

    # Atualizar configurações no script dinamicamente
    script_path = str(ROBO_SCRIPT)
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            codigo = f.read()

        import re
        codigo = re.sub(r'^WORKERS\s*=\s*\d+', f'WORKERS = {cfg.workers}', codigo, flags=re.M)
        codigo = re.sub(r'^USUARIO\s*=\s*"[^"]*"', f'USUARIO = "{cfg.usuario}"', codigo, flags=re.M)
        codigo = re.sub(r'^SENHA\s*=\s*"[^"]*"', f'SENHA = "{cfg.senha}"', codigo, flags=re.M)
        codigo = re.sub(r'^DELAY_POS\s*=\s*\d+', f'DELAY_POS = {cfg.delay}', codigo, flags=re.M)

        with open(script_path, "w", encoding="utf-8") as f:
            f.write(codigo)
    except Exception as e:
        raise HTTPException(500, f"Erro ao configurar robô: {e}")

    proc = subprocess.Popen(
        [sys.executable, "-u", script_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(BASE_DIR),
    )

    with estado_lock:
        estado["rodando"]  = True
        estado["processo"] = proc
        estado["workers"]  = cfg.workers
        estado["inicio"]   = datetime.now().isoformat()
        estado["erros"]    = 0
        estado["logs"]     = []

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
        estado["rodando"] = False

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
            with estado_lock:
                logs   = estado["logs"]
                novos  = logs[ultimo:]
                ultimo = len(logs)
                s = {
                    "rodando":    estado["rodando"],
                    "concluidos": estado["concluidos"],
                    "total":      contar_csv(ARQUIVO_CPF),
                    "repiques":   estado["repiques"],
                    "erros":      estado["erros"],
                    "logs":       novos,
                }
            yield f"data: {json.dumps(s, ensure_ascii=False)}\n\n"
            await asyncio.sleep(1.5)

    return StreamingResponse(gerador(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache",
                 "X-Accel-Buffering": "no"})


@app.get("/download/completo")
def download_completo():
    if not ARQUIVO_OUT.exists():
        raise HTTPException(404, "Arquivo não gerado ainda")
    return FileResponse(str(ARQUIVO_OUT),
        media_type="text/csv",
        filename="resultados_beneficios.csv")


@app.get("/download/repiques")
def download_repiques():
    if not ARQUIVO_REP.exists():
        raise HTTPException(404, "Arquivo de repiques não gerado ainda")
    return FileResponse(str(ARQUIVO_REP),
        media_type="text/csv",
        filename="Repiques_Ativos.csv")


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


@app.post("/upload/cpfs")
async def upload_cpfs(request):
    """Recebe novo arquivo de CPFs via POST."""
    body = await request.body()
    try:
        with open(ARQUIVO_CPF, "wb") as f:
            f.write(body)
        total = contar_csv(ARQUIVO_CPF)
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
