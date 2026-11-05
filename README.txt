DEUSA PSICO AI — versão nova

ARQUIVOS: app.py, requirements.txt, render.yaml.

RENDER:
Build Command: pip install -r requirements.txt
Start Command: gunicorn app:app --workers 1 --threads 4 --timeout 120

Environment Variables:
FLASK_SECRET_KEY = chave longa aleatória
PAINEL_SENHA = senha para entrar no painel
GOOGLE_CLIENT_SECRET_JSON = conteúdo completo do arquivo OAuth JSON baixado do Google Cloud
REDIRECT_URI = https://deusa-psico-ai.onrender.com/oauth2callback

GOOGLE CLOUD:
1. Ative YouTube Data API v3.
2. Crie OAuth Client ID do tipo Aplicativo da Web.
3. Authorized redirect URI deve ser exatamente:
https://deusa-psico-ai.onrender.com/oauth2callback
4. Configure a tela de consentimento e inclua a conta Google como usuário de teste se o app estiver em modo de teste.
5. Autorize a conta Google que administra ou modera o canal/chat.

IMPORTANTE:
O envio usa a API oficial do YouTube. A conta conectada precisa ter permissão de chat na live. O intervalo mínimo é 1 minuto; respeite modo lento, regras do canal e limites da API.
Render Free pode suspender o serviço e interromper a automação em segundo plano. O armazenamento local pode ser perdido em reinícios; para execução contínua, use hospedagem que não suspenda o processo e armazenamento persistente.
O código foi verificado estruturalmente, mas não foi testado com a conta Google nem em uma live real. Faça primeiro o teste de conexão.
