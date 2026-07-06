"""
Hapvida — Robô de Consulta (Paralelo)
Roda N "workers" (instâncias do Chrome) em paralelo, cada um logado
no portal do corretor, dividindo a lista de CPFs entre eles.

Uso normal (chamado automaticamente pelo servidor_local.py):
    python roboconsulta_paralelo.py --workers 12 --usuario 1374 --senha 123456 --delay 16

Também pode ser rodado sozinho (dá pra clicar duas vezes / rodar sem
argumentos) usando os valores padrão definidos abaixo.
"""

import argparse
import csv
import os
import re
import sys
import threading
import time

# ── Corrige encoding do console no Windows ────────────────────
# Sem isso, print() com emoji (🎯, etc.) quebra com
# "UnicodeEncodeError: 'charmap' codec can't encode character..."
# sempre que o stdout é capturado por outro processo (como faz o
# servidor_local.py), pois o Windows usa cp1252 por padrão nesse caso.
# Isso derrubava a consulta inteira e gravava "Erro na leitura" no CSV.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ── Configuração padrão (usada só se rodar sem argumentos) ───
WORKERS_PADRAO = 12
USUARIO_PADRAO = "1374"
SENHA_PADRAO   = "123456"
DELAY_PADRAO   = 16

ARQUIVO_CPF = "cpfs.csv"
ARQUIVO_OUT = "resultados_beneficios.csv"
ARQUIVO_REP = "Repiques_Ativos.csv"

URL_LOGIN    = "https://www.hapvida.com.br/corretor/"
URL_CONSULTA = "https://www.hapvida.com.br/corretor/pages/consulta/consulta_cliente.faces"

# ── Códigos de plano que contam como "Repique" ────────────────
# Lista tirada da tabela "REPIQUES — Código de Plano Repique".
# Um CPF entra como repique quando o campo "Plano" contém um desses
# códigos E não está cancelado. Se a lista da empresa mudar, basta
# editar aqui (mantenha os códigos como texto, entre aspas).
CODIGOS_REPIQUE = [
    "2708", "2721", "2737", "2738", "2741", "2751", "3780", "4897", "7735",
    "8867", "8745", "9667", "9732", "9919", "10528", "14958", "33108",
    "33042", "37167",
]
_CODIGOS_REPIQUE_SET = set(CODIGOS_REPIQUE)

CABECALHO     = ['CPF Buscado', 'Nome do Cliente', 'Contrato', 'Código do Usuário',
                  'Plano', 'Início', 'Cancelamento', 'Motivo do Cancelamento']
CABECALHO_REP = CABECALHO + ['Plano (Repique)']


def abrir_com_retry(caminho, mode, tentativas=8, espera=0.6, **kwargs):
    """open() com retry — necessário porque no Windows, se o CSV estiver
    aberto no Excel, o open() falha na hora com PermissionError (WinError
    32). Sem retry, uma única tentativa falhava e a gravação nem
    acontecia — o arquivo ficava com os dados da consulta anterior, dando
    a falsa impressão de que "não limpou". Com isso, esperamos um pouco
    e tentamos de novo (o lock costuma ser breve); se continuar
    bloqueado depois de todas as tentativas, deixamos o PermissionError
    subir para quem chamou tratar e avisar claramente o usuário."""
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        try:
            return open(caminho, mode, **kwargs)
        except PermissionError as e:
            ultimo_erro = e
            if tentativa < tentativas:
                time.sleep(espera)
    raise ultimo_erro

# ── Estado compartilhado entre as threads ─────────────────────
lock_out   = threading.Lock()
lock_rep   = threading.Lock()
lock_stat  = threading.Lock()
stop_event = threading.Event()

contadores = {"concluidos": 0, "repiques": 0, "erros": 0}
workers_status = {}   # id -> 'pendente' | 'ativo' | 'erro' | 'fim'


def parse_args():
    p = argparse.ArgumentParser(description="Robô de consulta Hapvida (paralelo)")
    p.add_argument("--workers", type=int, default=WORKERS_PADRAO)
    p.add_argument("--usuario", type=str, default=USUARIO_PADRAO)
    p.add_argument("--senha",   type=str, default=SENHA_PADRAO)
    p.add_argument("--delay",   type=int, default=DELAY_PADRAO)
    return p.parse_args()


def montar_driver():
    chrome_options = Options()
    chrome_options.add_experimental_option("detach", True)
    chrome_options.add_argument("--incognito")
    chrome_options.add_argument("--disable-features=PasswordLeakDetection")
    prefs = {"credentials_enable_service": False, "profile.password_manager_enabled": False}
    chrome_options.add_experimental_option("prefs", prefs)
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    driver = webdriver.Chrome(options=chrome_options)
    wait = WebDriverWait(driver, 20)
    return driver, wait


def eh_repique(nome_plano, cancelamento):
    """Repique = campo 'Plano' contém um dos códigos da lista CODIGOS_REPIQUE
    E o plano não está cancelado (campo 'Cancelamento' vazio)."""
    if cancelamento and cancelamento.strip():
        return False
    if not CODIGOS_REPIQUE or not nome_plano:
        return False
    codigos_no_texto = re.findall(r'\d+', nome_plano)
    return any(c in _CODIGOS_REPIQUE_SET for c in codigos_no_texto)


def emitir_status(total, workers_total):
    """Imprime uma linha padronizada que o servidor_local.py sabe interpretar."""
    with lock_stat:
        ativos    = sum(1 for s in workers_status.values() if s == 'ativo')
        pendentes = sum(1 for s in workers_status.values() if s == 'pendente')
        com_erro  = sum(1 for s in workers_status.values() if s == 'erro')
    concl = contadores["concluidos"]
    rep   = contadores["repiques"]
    err   = contadores["erros"]
    print(f">>> STATUS | total={total} concluidos={concl} repiques={rep} erros={err} "
          f"workers_ativos={ativos} workers_pendentes={pendentes} "
          f"workers_erro={com_erro} workers_total={workers_total}", flush=True)


def gravar_linhas(linhas):
    """Grava linhas no CSV principal (thread-safe) e replica repiques.
    Usa retry (abrir_com_retry) para o caso do arquivo estar aberto no
    Excel no momento da gravação — em vez de travar a thread inteira com
    uma exceção não tratada."""
    with lock_out:
        try:
            with abrir_com_retry(ARQUIVO_OUT, "a", newline="", encoding="utf-8-sig") as f:
                csv.writer(f, delimiter=";").writerows(linhas)
        except PermissionError:
            cpfs_perdidos = ", ".join(l[0] for l in linhas)
            print(f"⚠️ ATENÇÃO: '{ARQUIVO_OUT}' está aberto no Excel (ou outro programa) — "
                  f"não consegui gravar o(s) CPF(s) {cpfs_perdidos}. FECHE O ARQUIVO para não perder dados.",
                  flush=True)

    for linha in linhas:
        cpf, nome, contrato, cod, plano, inicio, cancel, motivo = linha
        if eh_repique(plano, cancel):
            with lock_rep:
                try:
                    with abrir_com_retry(ARQUIVO_REP, "a", newline="", encoding="utf-8-sig") as f:
                        csv.writer(f, delimiter=";").writerow(linha + [plano])
                except PermissionError:
                    print(f"⚠️ ATENÇÃO: '{ARQUIVO_REP}' está aberto no Excel (ou outro programa) — "
                          f"não consegui gravar o repique do CPF {cpf}. FECHE O ARQUIVO para não perder dados.",
                          flush=True)
                    continue
            with lock_stat:
                contadores["repiques"] += 1
            print(f"🎯 REPIQUE: {cpf} - {plano}", flush=True)


def processar_cpf(driver, wait, cpf_cliente):
    """Consulta um CPF e devolve a lista de linhas (uma por benefício encontrado)."""
    campo_cpf = wait.until(EC.presence_of_element_located(
        (By.XPATH, "//div[@id='formulario_busca:cpf']//input")))
    campo_cpf.click()
    time.sleep(0.5)
    campo_cpf.clear()
    campo_cpf.send_keys(cpf_cliente)
    time.sleep(1)

    driver.find_element(By.ID, "formulario_busca:btnPesquisa").click()
    time.sleep(5)

    linhas_tabela = driver.find_elements(By.XPATH, "//table[contains(@class, 'table')]/tbody/tr")

    if len(linhas_tabela) == 0:
        return [[cpf_cliente, "Nenhum benefício encontrado", "", "", "", "", "", ""]]

    linhas = []
    for tr in linhas_tabela:
        colunas = tr.find_elements(By.TAG_NAME, "td")
        if len(colunas) >= 8:
            nome         = colunas[0].text
            contrato     = colunas[2].text
            cod_usuario  = colunas[3].text
            plano        = colunas[4].text
            inicio       = colunas[5].text
            cancelamento = colunas[6].text
            motivo       = colunas[7].text
            linhas.append([cpf_cliente, nome, contrato, cod_usuario, plano,
                            inicio, cancelamento, motivo])

    if not linhas:
        linhas = [[cpf_cliente, "Nenhum benefício encontrado", "", "", "", "", "", ""]]
    return linhas


def worker_loop(wid, cpfs_atribuidos, usuario, senha, delay, total, workers_total):
    tag = f"[Worker {wid:02d}]"
    workers_status[wid] = 'pendente'
    driver = None
    wait = None

    try:
        print(f"{tag} Abrindo Chrome...", flush=True)
        driver, wait = montar_driver()
        driver.get(URL_LOGIN)

        print(f"{tag} Realizando login...", flush=True)
        wait.until(EC.presence_of_element_located(
            (By.ID, "login_form:username"))).send_keys(usuario)
        driver.find_element(By.ID, "login_form:password").send_keys(senha)
        time.sleep(1)
        driver.find_element(By.ID, "login_form:j_idt11").click()
        wait.until(EC.url_contains("home.xhtml"))

        driver.get(URL_CONSULTA)
        time.sleep(3)

        with lock_stat:
            workers_status[wid] = 'ativo'
        print(f"{tag} Login OK — {len(cpfs_atribuidos)} CPF(s) atribuído(s)", flush=True)
        emitir_status(total, workers_total)

    except Exception as e:
        with lock_stat:
            workers_status[wid] = 'erro'
        print(f"{tag} Login FALHOU: {e}", flush=True)
        emitir_status(total, workers_total)
        if driver:
            try: driver.quit()
            except Exception: pass
        return

    for cpf_cliente in cpfs_atribuidos:
        if stop_event.is_set():
            break
        cpf_cliente = str(cpf_cliente).strip()
        if not cpf_cliente:
            continue

        print(f"{tag} Pesquisando CPF: {cpf_cliente}", flush=True)
        try:
            linhas = processar_cpf(driver, wait, cpf_cliente)
            gravar_linhas(linhas)
            print(f"{tag} CPF {cpf_cliente} -> {len(linhas)} registro(s)", flush=True)
        except Exception as e:
            gravar_linhas([[cpf_cliente, "Erro na leitura", "", "", "", "", "", str(e)[:150]]])
            with lock_stat:
                contadores["erros"] += 1
            print(f"{tag} ERRO ao processar {cpf_cliente}: {e}", flush=True)
            try:
                driver.get(URL_CONSULTA)
                time.sleep(3)
            except Exception:
                pass

        with lock_stat:
            contadores["concluidos"] += 1
        emitir_status(total, workers_total)

        if stop_event.is_set():
            break

        # Pausa obrigatória do site — interrompível se o usuário mandar parar
        if stop_event.wait(delay):
            break

        try:
            driver.get(URL_CONSULTA)
            time.sleep(3)
        except Exception as e:
            print(f"{tag} Falha ao recarregar página: {e}", flush=True)

    with lock_stat:
        workers_status[wid] = 'fim'
    print(f"{tag} Encerrado.", flush=True)
    try:
        driver.quit()
    except Exception:
        pass


def monitor_periodico(total, workers_total):
    """Mantém o servidor informado em tempo real, mesmo entre uma consulta e outra
    (importante para mostrar workers logando antes da 1ª consulta terminar)."""
    while not stop_event.is_set():
        emitir_status(total, workers_total)
        if stop_event.wait(3):
            break


def main():
    args = parse_args()
    workers_solicitados = max(1, args.workers)

    print("--- INICIANDO ROBÔ (PARALELO) ---", flush=True)
    print(f"Workers solicitados: {workers_solicitados} | Delay pós-consulta: {args.delay}s", flush=True)

    if not CODIGOS_REPIQUE:
        print("[AVISO] CODIGOS_REPIQUE está vazio — nenhum CPF será marcado como repique. "
              "Edite a lista no topo de roboconsulta_paralelo.py.", flush=True)

    if not os.path.exists(ARQUIVO_CPF):
        print(f"\n[!] ALERTA: O arquivo '{ARQUIVO_CPF}' não foi encontrado!", flush=True)
        return

    with open(ARQUIVO_CPF, mode="r", encoding="utf-8") as f:
        cpfs = [str(l[0]).strip() for l in csv.reader(f) if l and str(l[0]).strip()]

    total = len(cpfs)
    if total == 0:
        print(f"\n[!] ALERTA: O arquivo '{ARQUIVO_CPF}' foi aberto, mas parece estar vazio!", flush=True)
        return

    # Não faz sentido abrir mais workers que CPFs a consultar
    workers_total = min(workers_solicitados, total)
    if workers_total < workers_solicitados:
        print(f"[AVISO] Apenas {total} CPF(s) na lista — reduzindo para {workers_total} worker(s).", flush=True)

    # Arquivos de saída recomeçam do zero a cada execução.
    # Se estiverem abertos no Excel (ou outro programa), o open() falha —
    # nesse caso ABORTAMOS aqui com um aviso claro, em vez de deixar o
    # robô "rodar" sem nunca ter limpado o arquivo antigo.
    try:
        with abrir_com_retry(ARQUIVO_OUT, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f, delimiter=";").writerow(CABECALHO)
        with abrir_com_retry(ARQUIVO_REP, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f, delimiter=";").writerow(CABECALHO_REP)
    except PermissionError:
        print(f"\n[!] ALERTA: Não consegui recomeçar '{ARQUIVO_OUT}' e/ou '{ARQUIVO_REP}' — "
              f"o arquivo está aberto no Excel (ou outro programa) neste PC.", flush=True)
        print("[!] FECHE O ARQUIVO NO EXCEL e clique em Iniciar novamente. "
              "Os dados da consulta anterior NÃO foram apagados.", flush=True)
        return

    # Distribui os CPFs entre os workers (round-robin, mantém carga equilibrada)
    lotes = [cpfs[i::workers_total] for i in range(workers_total)]
    for wid in range(1, workers_total + 1):
        workers_status[wid] = 'pendente'

    emitir_status(total, workers_total)

    threads = [
        threading.Thread(target=worker_loop, args=(
            wid, lote, args.usuario, args.senha, args.delay, total, workers_total), daemon=True)
        for wid, lote in enumerate(lotes, start=1)
    ]
    mon = threading.Thread(target=monitor_periodico, args=(total, workers_total), daemon=True)
    mon.start()

    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\n[!] Interrompido! Salvando progresso e encerrando workers com segurança...", flush=True)
        stop_event.set()
        for t in threads:
            t.join(timeout=15)

    stop_event.set()
    emitir_status(total, workers_total)

    if contadores["concluidos"] >= total:
        print("\nSUCESSO TOTAL! Todas as pesquisas foram concluídas e salvas.", flush=True)
    else:
        print(f"\nEncerrado com {contadores['concluidos']}/{total} CPF(s) processados.", flush=True)


if __name__ == "__main__":
    main()
