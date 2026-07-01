from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time
import csv
import os

# --- CONFIGURAÇÃO ---
chrome_options = Options()
chrome_options.add_experimental_option("detach", True)
chrome_options.add_argument("--incognito")
chrome_options.add_argument("--disable-features=PasswordLeakDetection")

prefs = {"credentials_enable_service": False, "profile.password_manager_enabled": False}
chrome_options.add_experimental_option("prefs", prefs)
chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])

driver = webdriver.Chrome(options=chrome_options)
wait = WebDriverWait(driver, 20)

try:
    print("--- INICIANDO ROBÔ ---")
    driver.get("https://www.hapvida.com.br/corretor/")
    
    # Login
    print("Realizando login...")
    wait.until(EC.presence_of_element_located((By.ID, "login_form:username"))).send_keys("1374")
    driver.find_element(By.ID, "login_form:password").send_keys("123456")
    time.sleep(1)
    driver.find_element(By.ID, "login_form:j_idt11").click()
    
    # Navega para a Consulta
    print("Acessando tela de Consulta Cliente...")
    wait.until(EC.url_contains("home.xhtml"))
    url_consulta = "https://www.hapvida.com.br/corretor/pages/consulta/consulta_cliente.faces"
    driver.get(url_consulta)
    
    print("Aguardando 3 segundos para o sistema do site liberar os campos...")
    time.sleep(3)
    
    if not os.path.exists('cpfs.csv'):
        print("\n[!] ALERTA: O arquivo 'cpfs.csv' nao foi encontrado!")
    else:
        print("Iniciando a leitura da planilha e criando o arquivo de saída...")
        
        # ABRINDO OS DOIS ARQUIVOS JUNTOS: O de leitura (CPFs) e o de escrita (Resultados)
        # O encoding='utf-8' agora é padrão, já que você criou no Bloco de Notas.
        with open('cpfs.csv', mode='r', encoding='utf-8') as arquivo_cpfs, \
             open('resultados_beneficios.csv', mode='w', newline='', encoding='utf-8-sig') as arquivo_saida:
            
            leitor = csv.reader(arquivo_cpfs)
            escritor = csv.writer(arquivo_saida, delimiter=';')
            
            # Escreve o cabeçalho imediatamente
            escritor.writerow(['CPF Buscado', 'Nome do Cliente', 'Contrato', 'Código do Usuário', 'Plano', 'Início', 'Cancelamento', 'Motivo do Cancelamento'])
            arquivo_saida.flush() # Força o Windows a salvar no disco imediatamente!
            
            cpfs_lidos = 0
            for linha in leitor:
                if not linha:
                    continue 
                
                cpf_cliente = str(linha[0]).strip()
                if not cpf_cliente:
                    continue

                cpfs_lidos += 1
                print(f"\n--- [{cpfs_lidos}] Pesquisando CPF: {cpf_cliente} ---")
                
                time.sleep(1)
                
                campo_cpf = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@id='formulario_busca:cpf']//input")))
                campo_cpf.click()
                time.sleep(0.5)
                campo_cpf.clear()
                campo_cpf.send_keys(cpf_cliente)
                
                time.sleep(1)
                
                print("Clicando no botão Pesquisar...")
                driver.find_element(By.ID, "formulario_busca:btnPesquisa").click()
                
                print("Aguardando sistema trazer os dados...")
                time.sleep(5) 
                
                try:
                    linhas_tabela = driver.find_elements(By.XPATH, "//table[contains(@class, 'table')]/tbody/tr")
                    
                    if len(linhas_tabela) == 0:
                         print("-> Nenhum benefício encontrado para este CPF.")
                         escritor.writerow([cpf_cliente, "Nenhum benefício encontrado", "", "", "", "", "", ""])
                    else:
                        print(f"-> Encontrado(s) {len(linhas_tabela)} registro(s)!")
                        for tr in linhas_tabela:
                            colunas = tr.find_elements(By.TAG_NAME, "td")
                            if len(colunas) >= 8:
                                nome = colunas[0].text
                                contrato = colunas[2].text
                                cod_usuario = colunas[3].text
                                plano = colunas[4].text
                                inicio = colunas[5].text
                                cancelamento = colunas[6].text
                                motivo = colunas[7].text
                                
                                # Grava a linha do cliente achado
                                escritor.writerow([cpf_cliente, nome, contrato, cod_usuario, plano, inicio, cancelamento, motivo])
                except Exception as e:
                    print(f"-> Erro ao extrair dados do CPF {cpf_cliente}: {e}")
                    escritor.writerow([cpf_cliente, "Erro na leitura", "", "", "", "", "", ""])
                
                # SALVA NO DISCO AGORA: Se o robô parar depois daqui, esse CPF já está salvo!
                arquivo_saida.flush() 
                
                # --- A PAUSA OBRIGATÓRIA DO SITE ---
                print("Aguardando 16 segundos obrigatórios do site para a próxima pesquisa...")
                time.sleep(16)
                
                print("Recarregando a página...")
                driver.get(url_consulta)
                time.sleep(3)

            if cpfs_lidos == 0:
                print("\n[!] ALERTA: O arquivo 'cpfs.csv' foi aberto, mas parece estar vazio!")
            else:
                print("\nSUCESSO TOTAL! Todas as pesquisas foram concluídas e salvas no Excel.")

# Essa parte garante que se você fechar a tela ou apertar Ctrl+C, ele avisa que o que foi feito está salvo
except KeyboardInterrupt:
    print("\n[!] Você interrompeu o robô! Não se preocupe, os CPFs pesquisados até agora foram salvos na planilha 'resultados_beneficios.csv'.")
except Exception as erro:
    print(f"\n[!] Ocorreu um erro inesperado no robô:\n{erro}")
    print("Os CPFs pesquisados antes do erro ocorrer já foram salvos na planilha.")