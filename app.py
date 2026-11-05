
import os
import re
import json
import time
import secrets
import threading
from datetime import datetime
from functools import wraps

from flask import Flask, request, redirect, url_for, session, flash, jsonify, render_template_string
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# =========================================================
# CONFIGURAÇÃO
# =========================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)

TOKEN_FILE = os.path.join(DATA_DIR, "token.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
MESSAGES_FILE = os.path.join(DATA_DIR, "messages.json")

SCOPES = [
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube.readonly"
]

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

lock = threading.RLock()
worker_thread = None
stop_event = threading.Event()
credentials = None

DEFAULT_SETTINGS = {
    "target": "",
    "interval": 5,
    "startup_enabled": True,
    "startup_message": "Olá! Obrigado por acompanhar a live!",
    "welcome_enabled": False,
    "welcome_message": "Bem-vindo(a), {nome}! Aproveite a live!",
    "link_enabled": False,
    "link_message": ""
}

runtime = {
    "running": False,
    "paused": False,
    "status": "Parado",
    "last_error": "",
    "last_check": "",
    "last_sent": "",
    "live_title": "",
    "live_id": "",
    "chat_id": "",
    "channel_title": "",
    "logs": [],
    "message_cursor": 0,
    "next_send": 0,
    "page_token": None,
    "seen_users": []
}


# =========================================================
# ARQUIVOS E HISTÓRICO
# =========================================================

def log(message, kind="info"):
    with lock:
        runtime["logs"].insert(0, {
            "time": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            "message": str(message)[:500],
            "kind": kind
        })
        runtime["logs"] = runtime["logs"][:80]


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        return default


def write_json(path, data):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def get_settings():
    data = read_json(SETTINGS_FILE, {})
    result = DEFAULT_SETTINGS.copy()
    if isinstance(data, dict):
        result.update(data)
    return result


def get_messages():
    data = read_json(MESSAGES_FILE, [])
    return data if isinstance(data, list) else []


# =========================================================
# GOOGLE OAUTH
# =========================================================

def redirect_uri():
    return os.environ.get(
        "REDIRECT_URI",
        request.url_root.rstrip("/") + "/oauth2callback"
    )


def get_google_config():
    raw = os.environ.get("GOOGLE_CLIENT_SECRET_JSON", "").strip()

    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise RuntimeError("O JSON do Google está inválido.")

    path = os.path.join(BASE_DIR, "client_secret.json")

    if os.path.exists(path):
        with open(path, encoding="utf-8") as file:
            return json.load(file)

    raise RuntimeError(
        "Configure GOOGLE_CLIENT_SECRET_JSON no Render."
    )


def save_credentials(value):
    global credentials
    credentials = value

    with open(TOKEN_FILE, "w", encoding="utf-8") as file:
        file.write(value.to_json())


def load_credentials():
    global credentials

    if credentials and credentials.valid:
        return credentials

    if not os.path.exists(TOKEN_FILE):
        return None

    try:
        value = Credentials.from_authorized_user_file(
            TOKEN_FILE, SCOPES
        )

        if value.expired and value.refresh_token:
            value.refresh(Request())
            save_credentials(value)

        if value.valid:
            credentials = value
            return value

    except Exception as error:
        log("Erro ao recuperar Google: " + str(error), "error")

    return None


def youtube():
    value = load_credentials()

    if not value:
        raise RuntimeError("Google não conectado.")

    return build(
        "youtube",
        "v3",
        credentials=value,
        cache_discovery=False
    )


# =========================================================
# IDENTIFICAÇÃO DE CANAL E LIVE
# =========================================================

def extract_video_id(value):
    value = (value or "").strip()

    patterns = [
        r"(?:youtube\.com/watch\?v=)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/live/)([A-Za-z0-9_-]{11})",
        r"(?:youtu\.be/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/shorts/)([A-Za-z0-9_-]{11})"
    ]

    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return match.group(1)

    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value

    return None


def resolve_channel(yt, value):
    value = (value or "").strip()

    if not value:
        raise ValueError("Informe o canal ou o link da live.")

    channel_match = re.search(r"(UC[A-Za-z0-9_-]{20,})", value)

    if channel_match:
        result = yt.channels().list(
            part="snippet",
            id=channel_match.group(1)
        ).execute()

    else:
        handle = value.rstrip("/").split("/")[-1]

        if "youtube.com" in value and "/@" in value:
            handle = "@" + value.split("/@")[-1].split("/")[0]

        if not handle.startswith("@") and "youtube.com" not in value:
            handle = "@" + handle

        if handle.startswith("@"):
            result = yt.channels().list(
                part="snippet",
                forHandle=handle
            ).execute()
        else:
            result = yt.search().list(
                part="snippet",
                q=handle,
                type="channel",
                maxResults=5
            ).execute()

            ids = [
                item["snippet"]["channelId"]
                for item in result.get("items", [])
            ]

            result = yt.channels().list(
                part="snippet",
                id=",".join(ids)
            ).execute() if ids else {"items": []}

    items = result.get("items", [])

    if not items:
        raise RuntimeError("Não encontrei esse canal.")

    return (
        items[0]["id"],
        items[0]["snippet"].get("title", "Canal")
    )


def inspect_video(yt, video_id):
    result = yt.videos().list(
        part="snippet,liveStreamingDetails",
        id=video_id
    ).execute()

    items = result.get("items", [])

    if not items:
        return None

    video = items[0]
    details = video.get("liveStreamingDetails", {})
    live_state = video["snippet"].get("liveBroadcastContent")

    is_live = (
        live_state == "live"
        or bool(details.get("actualStartTime"))
        and not details.get("actualEndTime")
    )

    if not is_live:
        return {
            "found": False,
            "video_id": video_id,
            "title": video["snippet"].get("title", "Live"),
            "chat_id": None
        }

    return {
        "found": True,
        "video_id": video_id,
        "title": video["snippet"].get("title", "Live"),
        "chat_id": details.get("activeLiveChatId")
    }


def find_live(yt, target):
    video_id = extract_video_id(target)

    if video_id:
        return inspect_video(yt, video_id)

    channel_id, channel_title = resolve_channel(yt, target)

    result = yt.search().list(
        part="id,snippet",
        channelId=channel_id,
        eventType="live",
        type="video",
        maxResults=5
    ).execute()

    for item in result.get("items", []):
        video_id = item["id"]["videoId"]
        live = inspect_video(yt, video_id)

        if live and live["found"]:
            live["channel_title"] = channel_title
            return live

    return {
        "found": False,
        "channel_title": channel_title
    }


# =========================================================
# ENVIO E LEITURA DO CHAT
# =========================================================

def send_message(yt, chat_id, message):
    message = (message or "").strip()

    if not message:
        raise ValueError("A mensagem está vazia.")

    if len(message) > 200:
        raise ValueError("A mensagem ultrapassa 200 caracteres.")

    yt.liveChatMessages().insert(
        part="snippet",
        body={
            "snippet": {
                "liveChatId": chat_id,
                "type": "textMessageEvent",
                "textMessageDetails": {
                    "messageText": message
                }
            }
        }
    ).execute()

    runtime["last_sent"] = datetime.now().strftime(
        "%d/%m/%Y %H:%M:%S"
    )

    log("Mensagem enviada: " + message)


def poll_chat(yt, chat_id):
    args = {
        "liveChatId": chat_id,
        "part": "snippet,authorDetails",
        "maxResults": 200
    }

    if runtime["page_token"]:
        args["pageToken"] = runtime["page_token"]

    result = yt.liveChatMessages().list(**args).execute()

    runtime["page_token"] = result.get("nextPageToken")

    wait = max(
        5,
        int(result.get("pollingIntervalMillis", 10000)) / 1000
    )

    settings = get_settings()

    if settings["welcome_enabled"]:
        seen = set(runtime["seen_users"])

        for item in result.get("items", []):
            author = item.get("authorDetails", {})
            user_id = author.get("channelId")
            name = author.get("displayName", "pessoa")

            if not user_id or user_id in seen:
                continue

            if (
                author.get("isChatOwner")
                or author.get("isChatModerator")
            ):
                continue

            message = settings["welcome_message"].replace(
                "{nome}", name
            )

            if message.strip():
                send_message(yt, chat_id, message)
                seen.add(user_id)
                runtime["seen_users"] = list(seen)[-500:]

            break

    return wait


# =========================================================
# AUTOMAÇÃO
# =========================================================

def worker():
    runtime.update({
        "running": True,
        "paused": False,
        "status": "Procurando live",
        "last_error": ""
    })

    log("Automação iniciada.")

    while not stop_event.is_set():

        if runtime["paused"]:
            time.sleep(1)
            continue

        try:
            settings = get_settings()
            yt = youtube()

            live = find_live(yt, settings["target"])

            runtime["last_check"] = datetime.now().strftime(
                "%d/%m/%Y %H:%M:%S"
            )

            if not live or not live.get("found"):
                runtime.update({
                    "status": "Aguardando live",
                    "live_id": "",
                    "chat_id": "",
                    "live_title": ""
                })

                time.sleep(20)
                continue

            video_id = live["video_id"]
            chat_id = live.get("chat_id")

            new_live = runtime["live_id"] != video_id

            runtime.update({
                "live_id": video_id,
                "live_title": live["title"],
                "chat_id": chat_id or "",
                "channel_title": live.get("channel_title", ""),
                "status": (
                    "Live encontrada — chat disponível"
                    if chat_id
                    else "Live encontrada — chat indisponível"
                )
            })

            if not chat_id:
                time.sleep(15)
                continue

            if new_live:
                runtime["seen_users"] = []
                runtime["page_token"] = None
                runtime["next_send"] = 0

                log("Nova live identificada: " + live["title"])

                if (
                    settings["startup_enabled"]
                    and settings["startup_message"].strip()
                ):
                    send_message(
                        yt,
                        chat_id,
                        settings["startup_message"]
                    )

            runtime["status"] = "Automação ativa — enviando mensagens"

            wait = 8

            if settings["welcome_enabled"]:
                try:
                    wait = poll_chat(yt, chat_id)
                except HttpError as error:
                    log("Erro ao consultar participantes: " + str(error), "error")

            now = time.time()

            if now >= runtime["next_send"]:

                messages = [
                    item for item in get_messages()
                    if item.get("enabled", True)
                    and item.get("text", "").strip()
                ]

                if messages:
                    index = runtime["message_cursor"] % len(messages)
                    message = messages[index]["text"]

                    send_message(yt, chat_id, message)

                    runtime["message_cursor"] += 1

                    runtime["next_send"] = (
                        now + max(1, int(settings["interval"])) * 60
                    )

                elif (
                    settings["link_enabled"]
                    and settings["link_message"].strip()
                ):
                    send_message(
                        yt,
                        chat_id,
                        settings["link_message"]
                    )

                    runtime["next_send"] = (
                        now + max(1, int(settings["interval"])) * 60
                    )

            time.sleep(min(max(wait, 5), 15))

        except Exception as error:
            runtime["last_error"] = str(error)
            runtime["status"] = "Erro — veja o histórico"
            log(str(error), "error")
            time.sleep(15)

    runtime.update({
        "running": False,
        "paused": False,
        "status": "Parado"
    })

    log("Automação parada.")


def start_worker():
    global worker_thread

    if worker_thread and worker_thread.is_alive():
        runtime["paused"] = False
        runtime["status"] = "Automação retomada"
        log("Automação continuada.")
        return

    stop_event.clear()

    worker_thread = threading.Thread(
        target=worker,
        daemon=True
    )

    worker_thread.start()


# =========================================================
# LOGIN
# =========================================================

def login_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return function(*args, **kwargs)
    return wrapper


LOGIN_HTML = """
<!DOCTYPE html>
<html lang="pt-br">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DEUSA PSICO AI</title>
<style>
body{background:#090b12;color:white;font-family:Arial;display:grid;place-items:center;min-height:100vh}
.card{background:#141824;padding:30px;border-radius:15px;width:300px}
input,button{width:100%;padding:13px;margin-top:12px;border-radius:8px;box-sizing:border-box}
input{background:#090b12;color:white;border:1px solid #343c50}
button{background:#7657e8;color:white;border:0;font-weight:bold;cursor:pointer}
</style>
<div class="card">
<h2>DEUSA PSICO AI</h2>
<p>Acesso ao painel</p>
{% for message in get_flashed_messages() %}
<p>{{message}}</p>
{% endfor %}
<form method="post">
<input type="password" name="password" placeholder="Senha do painel" required>
<button>ENTRAR</button>
</form>
</div>
</html>
"""


# =========================================================
# PAINEL
# =========================================================

PANEL_HTML = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DEUSA PSICO AI</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#090b12;color:#eef0f8;font:14px Arial}
main{max-width:1100px;margin:auto;padding:22px}
.panel,.stat{background:#141824;border:1px solid #282f40;border-radius:13px;padding:17px;margin-bottom:15px}
h1{font-size:24px}
h2{font-size:17px}
.muted{color:#9aa3b7}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.stat strong{display:block;margin-top:9px;font-size:16px}
input,textarea{width:100%;background:#0c111b;color:white;border:1px solid #343c50;border-radius:8px;padding:11px;margin:6px 0}
textarea{min-height:65px}
button,.btn{display:inline-block;background:#7657e8;color:white;border:0;border-radius:8px;padding:11px 14px;font-weight:bold;text-decoration:none;cursor:pointer;margin:4px 2px}
.secondary{background:#293247}
.green{background:#217b58}
.red{background:#873a48}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.field{flex:1;min-width:200px}
table{width:100%;border-collapse:collapse}
td,th{text-align:left;padding:9px;border-bottom:1px solid #282f40}
.log{padding:8px;border-bottom:1px solid #282f40}
.error{color:#ff8585}
.flash{padding:10px;background:#25203b;border-radius:8px;margin-bottom:10px}
@media(max-width:650px){.grid{grid-template-columns:1fr}main{padding:13px}}
</style>
</head>
<body>
<main>
<h1>DEUSA PSICO AI</h1>
<p class="muted">Painel de automação do YouTube</p>

{% for message in get_flashed_messages() %}
<div class="flash">{{message}}</div>
{% endfor %}

<section class="grid">
<div class="stat">Google<strong id="google">{{"Conectado" if connected else "Não conectado"}}</strong></div>
<div class="stat">Automação<strong id="bot">{{runtime.status}}</strong></div>
<div class="stat">Live<strong id="live">{{runtime.live_title or "Nenhuma live encontrada"}}</strong></div>
</section>

<section class="panel">
<h2>1. Google e live</h2>

{% if connected %}
<p class="good">Google conectado</p>
<a class="btn secondary" href="/google/disconnect">DESCONECTAR</a>
{% else %}
<a class="btn" href="/google/connect">CONECTAR GOOGLE</a>
{% endif %}

<form method="post" action="/target">
<label>Link da live, ID da live, link do canal ou @ do canal</label>
<input name="target" value="{{settings.target}}" placeholder="https://youtube.com/live/ID ou @canal" required>
<button>SALVAR DESTINO</button>
</form>

<form method="post" action="/check">
<button class="secondary">VERIFICAR LIVE E CHAT</button>
</form>

<p class="muted" id="liveid">ID da live: {{runtime.live_id or "—"}}</p>
<p class="muted" id="chatstatus">Chat: {{ "Disponível" if runtime.chat_id else "Aguardando identificação" }}</p>

<form method="post" action="/test-send">
<input name="test_message" maxlength="200" placeholder="Mensagem para testar o envio">
<button class="secondary">TESTAR ENVIO</button>
</form>
</section>

<section class="panel">
<h2>2. Mensagens personalizadas</h2>

<form method="post" action="/upload" enctype="multipart/form-data">
<label>Importar arquivo TXT — uma mensagem por linha</label>
<input type="file" name="file" accept=".txt" required>
<button>IMPORTAR TXT</button>
</form>

<form method="post" action="/messages/add">
<input name="text" maxlength="200" placeholder="Digite uma mensagem" required>
<button>ADICIONAR MENSAGEM</button>
</form>

<div style="overflow:auto">
<table>
<tr><th>Mensagem</th><th>Ativa</th><th>Ações</th></tr>
{% for item in messages %}
<tr>
<td>
<form id="edit{{item.id}}" method="post" action="/messages/edit/{{item.id}}">
<input name="text" value="{{item.text}}" maxlength="200">
</form>
</td>
<td>{{"Sim" if item.enabled else "Não"}}</td>
<td>
<button form="edit{{item.id}}">SALVAR</button>
<form style="display:inline" method="post" action="/messages/toggle/{{item.id}}">
<button class="secondary">{{"DESATIVAR" if item.enabled else "ATIVAR"}}</button>
</form>
<form style="display:inline" method="post" action="/messages/delete/{{item.id}}">
<button class="red">EXCLUIR</button>
</form>
</td>
</tr>
{% else %}
<tr><td colspan="3">Nenhuma mensagem cadastrada.</td></tr>
{% endfor %}
</table>
</div>
</section>

<section class="panel">
<h2>3. Configurações</h2>
<form method="post" action="/settings">

<label>Intervalo entre mensagens (minutos)</label>
<input type="number" name="interval" min="1" max="180" value="{{settings.interval}}">

<label><input style="width:auto" type="checkbox" name="startup_enabled" {% if settings.startup_enabled %}checked{% endif %}> Mensagem ao encontrar nova live</label>
<textarea name="startup_message">{{settings.startup_message}}</textarea>

<label><input style="width:auto" type="checkbox" name="welcome_enabled" {% if settings.welcome_enabled %}checked{% endif %}> Boas-vindas a novos participantes</label>
<textarea name="welcome_message">{{settings.welcome_message}}</textarea>
<p class="muted">Use {nome} para inserir o nome público.</p>

<label><input style="width:auto" type="checkbox" name="link_enabled" {% if settings.link_enabled %}checked{% endif %}> Enviar mensagem com link quando não houver mensagens ativas</label>
<input name="link_message" value="{{settings.link_message}}" maxlength="200">

<button>SALVAR CONFIGURAÇÕES</button>
</form>
</section>

<section class="panel">
<h2>4. Controles</h2>
<div class="row">
<form method="post" action="/start"><button class="green">INICIAR</button></form>
<form method="post" action="/pause"><button class="secondary">PAUSAR</button></form>
<form method="post" action="/resume"><button>CONTINUAR</button></form>
<form method="post" action="/stop"><button class="red">PARAR</button></form>
</div>
</section>

<section class="panel">
<h2>5. Histórico e erros</h2>
<div id="logs">
{% for item in runtime.logs %}
<div class="log {{item.kind}}"><b>{{item.time}}</b> — {{item.message}}</div>
{% else %}
<p class="muted">Nenhum registro.</p>
{% endfor %}
</div>
<p class="muted" id="lastcheck">Última verificação: {{runtime.last_check or "—"}}</p>
<p class="muted" id="lastsent">Último envio: {{runtime.last_sent or "—"}}</p>
</section>

<p><a class="btn secondary" href="/logout">SAIR</a></p>
</main>

<script>
async function updatePanel(){
 try{
  const response=await fetch("/status");
  const data=await response.json();
  document.getElementById("bot").textContent=data.status;
  document.getElementById("live").textContent=data.live_title||"Nenhuma live encontrada";
  document.getElementById("liveid").textContent="ID da live: "+(data.live_id||"—");
  document.getElementById("chatstatus").textContent="Chat: "+(data.chat_id?"Disponível":"Indisponível ou aguardando");
  document.getElementById("lastcheck").textContent="Última verificação: "+(data.last_check||"—");
  document.getElementById("lastsent").textContent="Último envio: "+(data.last_sent||"—");
  document.getElementById("logs").innerHTML=data.logs.map(item=>
   '<div class="log '+item.kind+'"><b>'+item.time+'</b> — '+item.message.replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;")+'</div>'
  ).join("");
 }catch(e){}
}
setInterval(updatePanel,3000);
updatePanel();
</script>
</body>
</html>
"""


# =========================================================
# ROTAS
# =========================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        expected = os.environ.get("PAINEL_SENHA", "")

        if expected and secrets.compare_digest(
            request.form.get("password", ""),
            expected
        ):
            session["logged_in"] = True
            return redirect(url_for("home"))

        flash("Senha incorreta ou PAINEL_SENHA não configurada.")

    return render_template_string(LOGIN_HTML)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def home():
    return render_template_string(
        PANEL_HTML,
        connected=bool(load_credentials()),
        settings=get_settings(),
        messages=get_messages(),
        runtime=runtime
    )


@app.route("/google/connect")
@login_required
def google_connect():
    try:
        flow = Flow.from_client_config(
            get_google_config(),
            scopes=SCOPES,
            redirect_uri=redirect_uri(),
            autogenerate_code_verifier=True
        )

        authorization_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent"
        )

        session["oauth_state"] = state
        session["oauth_verifier"] = flow.code_verifier

        return redirect(authorization_url)

    except Exception as error:
        flash("Erro ao conectar Google: " + str(error))
        return redirect(url_for("home"))


@app.route("/oauth2callback")
@login_required
def oauth_callback():
    try:
        state = session.get("oauth_state")
        verifier = session.get("oauth_verifier")

        if not state or not verifier:
            raise RuntimeError(
                "Sessão OAuth incompleta. Tente conectar novamente."
            )

        flow = Flow.from_client_config(
            get_google_config(),
            scopes=SCOPES,
            state=state,
            redirect_uri=redirect_uri(),
            code_verifier=verifier
        )

        flow.fetch_token(authorization_response=request.url)

        save_credentials(flow.credentials)

        session.pop("oauth_state", None)
        session.pop("oauth_verifier", None)

        log("Google conectado com sucesso.")
        flash("Google conectado com sucesso!")

    except Exception as error:
        log("Erro OAuth: " + str(error), "error")
        flash("Falha ao conectar: " + str(error))

    return redirect(url_for("home"))


@app.route("/google/disconnect")
@login_required
def google_disconnect():
    global credentials
    credentials = None

    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)

    flash("Google desconectado.")
    log("Google desconectado.")

    return redirect(url_for("home"))


@app.route("/target", methods=["POST"])
@login_required
def save_target():
    settings = get_settings()
    settings["target"] = request.form.get("target", "").strip()
    write_json(SETTINGS_FILE, settings)

    flash("Destino salvo.")
    return redirect(url_for("home"))


@app.route("/settings", methods=["POST"])
@login_required
def save_settings():
    settings = get_settings()

    settings["interval"] = max(
        1,
        min(180, int(request.form.get("interval", 5) or 5))
    )

    for key in (
        "startup_enabled",
        "welcome_enabled",
        "link_enabled"
    ):
        settings[key] = key in request.form

    for key in (
        "startup_message",
        "welcome_message",
        "link_message"
    ):
        settings[key] = request.form.get(key, "")[:200]

    write_json(SETTINGS_FILE, settings)

    flash("Configurações salvas.")
    return redirect(url_for("home"))


@app.route("/messages/add", methods=["POST"])
@login_required
def add_message():
    value = request.form.get("text", "").strip()

    if value:
        messages = get_messages()
        messages.append({
            "id": secrets.token_hex(6),
            "text": value[:200],
            "enabled": True
        })
        write_json(MESSAGES_FILE, messages)

    return redirect(url_for("home"))


@app.route("/messages/edit/<mid>", methods=["POST"])
@login_required
def edit_message(mid):
    messages = get_messages()

    for item in messages:
        if item["id"] == mid:
            item["text"] = request.form.get("text", "").strip()[:200]

    write_json(MESSAGES_FILE, messages)
    return redirect(url_for("home"))


@app.route("/messages/toggle/<mid>", methods=["POST"])
@login_required
def toggle_message(mid):
    messages = get_messages()

    for item in messages:
        if item["id"] == mid:
            item["enabled"] = not item.get("enabled", True)

    write_json(MESSAGES_FILE, messages)
    return redirect(url_for("home"))


@app.route("/messages/delete/<mid>", methods=["POST"])
@login_required
def delete_message(mid):
    messages = [
        item for item in get_messages()
        if item["id"] != mid
    ]

    write_json(MESSAGES_FILE, messages)
    return redirect(url_for("home"))


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    file = request.files.get("file")

    if not file or not file.filename.lower().endswith(".txt"):
        flash("Envie um arquivo TXT.")
        return redirect(url_for("home"))

    try:
        content = file.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("O arquivo precisa estar em UTF-8.")
        return redirect(url_for("home"))

    messages = get_messages()

    for line in content.splitlines():
        if line.strip():
            messages.append({
                "id": secrets.token_hex(6),
                "text": line.strip()[:200],
                "enabled": True
            })

    write_json(MESSAGES_FILE, messages)

    flash("Mensagens importadas.")
    return redirect(url_for("home"))


@app.route("/check", methods=["POST"])
@login_required
def check():
    try:
        yt = youtube()
        target = get_settings()["target"]

        live = find_live(yt, target)

        runtime["last_check"] = datetime.now().strftime(
            "%d/%m/%Y %H:%M:%S"
        )

        if not live or not live.get("found"):
            runtime.update({
                "status": "Nenhuma live ativa encontrada",
                "live_id": "",
                "chat_id": "",
                "live_title": ""
            })

            flash("Não encontrei uma transmissão ao vivo ativa.")

        elif not live.get("chat_id"):
            runtime.update({
                "status": "Live encontrada, mas sem chat ativo",
                "live_id": live["video_id"],
                "chat_id": "",
                "live_title": live["title"]
            })

            flash("Live encontrada, mas o chat não está disponível.")

        else:
            runtime.update({
                "status": "Live encontrada — chat disponível",
                "live_id": live["video_id"],
                "chat_id": live["chat_id"],
                "live_title": live["title"]
            })

            flash("Live encontrada e chat disponível!")

    except Exception as error:
        runtime["last_error"] = str(error)
        log("Falha na verificação: " + str(error), "error")
        flash("Erro ao verificar: " + str(error))

    return redirect(url_for("home"))


@app.route("/test-send", methods=["POST"])
@login_required
def test_send():
    try:
        yt = youtube()
        target = get_settings()["target"]
        live = find_live(yt, target)

        if not live or not live.get("found"):
            raise RuntimeError("Nenhuma live ativa encontrada.")

        if not live.get("chat_id"):
            raise RuntimeError("A live não possui chat disponível.")

        message = request.form.get(
            "test_message",
            "Teste de conexão do DEUSA PSICO AI!"
        ).strip()

        if not message:
            message = "Teste de conexão do DEUSA PSICO AI!"

        send_message(yt, live["chat_id"], message[:200])

        flash("Mensagem de teste enviada!")

    except Exception as error:
        runtime["last_error"] = str(error)
        log("Falha no teste de envio: " + str(error), "error")
        flash("Não foi possível enviar: " + str(error))

    return redirect(url_for("home"))


@app.route("/start", methods=["POST"])
@login_required
def start():
    if not load_credentials():
        flash("Conecte o Google primeiro.")
    elif not get_settings()["target"]:
        flash("Informe o canal ou link da live primeiro.")
    else:
        start_worker()
        flash("Automação iniciada.")

    return redirect(url_for("home"))


@app.route("/pause", methods=["POST"])
@login_required
def pause():
    runtime["paused"] = True
    runtime["status"] = "Pausado"
    log("Automação pausada.")

    return redirect(url_for("home"))


@app.route("/resume", methods=["POST"])
@login_required
def resume():
    if worker_thread and worker_thread.is_alive():
        runtime["paused"] = False
        runtime["status"] = "Automação retomada"
        log("Automação continuada.")
    else:
        start_worker()

    return redirect(url_for("home"))


@app.route("/stop", methods=["POST"])
@login_required
def stop():
    stop_event.set()
    runtime["status"] = "Parando"
    log("Solicitação para parar automação.")

    return redirect(url_for("home"))


@app.route("/status")
@login_required
def status():
    return jsonify({
        "running": runtime["running"],
        "paused": runtime["paused"],
        "status": runtime["status"],
        "live_title": runtime["live_title"],
        "live_id": runtime["live_id"],
        "chat_id": runtime["chat_id"],
        "last_check": runtime["last_check"],
        "last_sent": runtime["last_sent"],
        "last_error": runtime["last_error"],
        "logs": runtime["logs"][:30]
    })


@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "service": "DEUSA PSICO AI"
    })


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )
