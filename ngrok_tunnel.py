"""
Hapvida — Túnel ngrok
Expõe o servidor local para a internet.
"""
import time

try:
    from pyngrok import ngrok

    print("  Conectando ao ngrok...")
    tunnel = ngrok.connect(8000, proto="http")
    url = tunnel.public_url

    print()
    print("  =====================================================")
    print("  ✅ SERVIDOR ONLINE!")
    print()
    print("  URL PUBLICA:")
    print(f"  {url}")
    print()
    print("  Cole essa URL no campo API do dashboard!")
    print("  =====================================================")
    print()

    # Salvar URL em arquivo
    with open("url_publica.txt", "w") as f:
        f.write(url)
    print(f"  URL salva em: url_publica.txt")
    print()
    print("  DEIXE ESTA JANELA ABERTA enquanto usar o dashboard.")
    print("  Pressione Ctrl+C para encerrar.")
    print()

    while True:
        time.sleep(30)

except KeyboardInterrupt:
    print()
    print("  Servidor encerrado pelo usuario.")

except Exception as e:
    print(f"  Erro ao iniciar ngrok: {e}")
    print()
    print("  O servidor ainda esta rodando localmente em:")
    print("  http://localhost:8000")
    print()
    print("  Para acesso externo, instale o ngrok manualmente:")
    print("  https://ngrok.com/download")
    print()
    input("  Pressione Enter para sair...")
