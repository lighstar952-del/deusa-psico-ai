
import os, json, time, threading, secrets, re
from datetime import datetime
from functools import wraps
from flask import Flask, request, redirect, url_for, session, flash, render_template_string, jsonify
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(APP_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)
MESSAGES_FILE, STATE_FILE, TOKEN_FILE = [os.path.join(DATA_DIR, x) for x in ("messages.json", "settings.json", "token.json")]
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl", "https://www.googleapis.com/auth/youtube.readonly"]
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
lock = threading.RLock()
worker_thread = None
worker_stop = threading.Event()
runtime = {"running": False, "paused": False, "status": "Parado", "last_error": "", "last_check": "",
           "last_sent": "", "live_title": "", "live_id": "", "chat_id": "", "seen_users": [],
           "next_send": 0, "page_token": None, "logs": []}
creds = None
DEFAULT_SETTINGS = {"channel": "", "interval": 5, "startup_enabled": True,
 "startup_message": "Olá! Obrigado por acompanhar a live!", "welcome_enabled": False,
 "welcome_message": "Bem-vindo(a), {nome}! Aproveite a live!", "link_message": "", "link_enabled": False}

def log(message, kind="info"):
    with lock:
        runtime["logs"].insert(0, {"time": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                                   "message": str(message)[:500], "kind": kind})
        runtime["logs"] = runtime["logs"][:100]

def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except Exception: return default

def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def get_messages():
    data = read_json(MESSAGES_FILE, [])
    return data if isinstance(data, list) else []

def get_settings():
    data = read_json(STATE_FILE, DEFAULT_SETTINGS.copy())
    result = DEFAULT_SETTINGS.copy()
    if isinstance(data, dict): result.update(data)
    return result

def get_client_config():
    raw = os.environ.get("GOOGLE_CLIENT_SECRET_JSON", "").strip()
    if raw:
        try: return json.loads(raw)
        except json.JSONDecodeError: raise RuntimeError("GOOGLE_CLIENT_SECRET_JSON não contém JSON válido.")
    path = os.path.join(APP_DIR, "client_secret.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f: return json.load(f)
    raise RuntimeError("Configure GOOGLE_CLIENT_SECRET_JSON no Render com o conteúdo do arquivo OAuth JSON.")

def redirect_uri():
    return os.environ.get("REDIRECT_URI", request.url_root.rstrip("/") + "/oauth2callback")

def load_credentials():
    global creds
    if creds and creds.valid: return creds
    if os.path.exists(TOKEN_FILE):
        try:
            c = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
            if c.expired and c.refresh_token:
                c.refresh(GoogleRequest())
                save_credentials(c)
            if c.valid:
                creds = c
                return c
        except Exception as e: log("Falha ao recuperar autorização Google: " + str(e), "error")
    return None

def save_credentials(c):
    global creds
    creds = c
    with open(TOKEN_FILE, "w", encoding="utf-8") as f: f.write(c.to_json())

def yt_client():
    c = load_credentials()
    if not c: raise RuntimeError("Google não conectado. Clique em CONECTAR GOOGLE.")
    return build("youtube", "v3", credentials=c, cache_discovery=False)

def login_required(fn):
    @wraps(fn)
    def wrapped(*a, **kw):
        if not session.get("logged_in"): return redirect(url_for("login"))
        return fn(*a, **kw)
    return wrapped

def resolve_channel(yt, value):
    value = (value or "").strip()
    if not value: raise ValueError("Informe o link ou identificador do canal.")
    m = re.search(r"(UC[\w-]{20,})", value)
    if m:
        result = yt.channels().list(part="snippet", id=m.group(1), maxResults=1).execute()
    else:
        handle = value.rstrip("/").split("/")[-1]
        if not handle.startswith("@") and "youtube.com" not in value: handle = "@" + handle
        if handle.startswith("@"):
            result = yt.channels().list(part="snippet", forHandle=handle, maxResults=1).execute()
        else:
            sr = yt.search().list(part="snippet", q=handle, type="channel", maxResults=5).execute()
            ids = [x["snippet"]["channelId"] for x in sr.get("items", [])]
            result = yt.channels().list(part="snippet", id=",".join(ids), maxResults=5).execute() if ids else {"items": []}
    items = result.get("items", [])
    if not items: raise RuntimeError("Canal não encontrado. Tente o link completo ou @identificador.")
    return items[0]["id"], items[0]["snippet"].get("title", "Canal")

def find_live(yt, channel_id):
    sr = yt.search().list(part="id,snippet", channelId=channel_id, eventType="live", type="video", maxResults=5).execute()
    for item in sr.get("items", []):
        vid = item["id"]["videoId"]
        vr = yt.videos().list(part="snippet,liveStreamingDetails", id=vid).execute()
        for video in vr.get("items", []):
            chat = video.get("liveStreamingDetails", {}).get("activeLiveChatId")
            if chat:
                return {"video_id": vid, "chat_id": chat, "title": video["snippet"].get("title", "Live")}
    return None

def send_chat(yt, chat_id, message):
    yt.liveChatMessages().insert(part="snippet", body={"snippet": {"liveChatId": chat_id,
        "type": "textMessageEvent", "textMessageDetails": {"messageText": message[:200]}}}).execute()

def check_live():
    yt = yt_client()
    cid, channel_title = resolve_channel(yt, get_settings().get("channel", ""))
    live = find_live(yt, cid)
    runtime["last_check"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    if not live:
        runtime.update({"live_id": "", "chat_id": "", "live_title": "", "status": "Canal conectado — nenhuma live com chat ativo"})
        return {"channel": channel_title, "live": False}
    runtime.update({"live_id": live["video_id"], "chat_id": live["chat_id"], "live_title": live["title"],
                    "status": "Live encontrada — chat disponível"})
    return {"channel": channel_title, "live": True, **live}

def poll_chat(yt, chat_id):
    try:
        args = {"liveChatId": chat_id, "part": "snippet,authorDetails", "maxResults": 200}
        if runtime.get("page_token"): args["pageToken"] = runtime["page_token"]
        result = yt.liveChatMessages().list(**args).execute()
        runtime["page_token"] = result.get("nextPageToken")
        wait = max(5, int(result.get("pollingIntervalMillis", 10000)) / 1000)
        s = get_settings()
        if s.get("welcome_enabled"):
            seen = set(runtime.get("seen_users", []))
            for item in result.get("items", []):
                author = item.get("authorDetails", {})
                uid, name = author.get("channelId"), author.get("displayName", "pessoa")
                if uid and uid not in seen and not author.get("isChatOwner") and not author.get("isChatModerator"):
                    message = s.get("welcome_message", "").replace("{nome}", name)
                    if message.strip():
                        send_chat(yt, chat_id, message)
                        seen.add(uid); runtime["seen_users"] = list(seen)[-500:]
                        log("Boas-vindas enviada para " + name)
                    break
        return wait
    except HttpError as e:
        log("Erro consultando chat: " + str(e), "error")
        return 15

def worker():
    runtime.update({"running": True, "paused": False, "status": "Iniciando"})
    log("Automação iniciada.")
    while not worker_stop.is_set():
        if runtime["paused"]:
            time.sleep(1); continue
        try:
            s = get_settings()
            yt = yt_client()
            cid, channel_title = resolve_channel(yt, s.get("channel", ""))
            live = find_live(yt, cid)
            runtime["last_check"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            if not live:
                runtime.update({"status": "Aguardando live com chat ativo", "live_id": "", "chat_id": "", "live_title": ""})
                time.sleep(20); continue
            new_live = runtime.get("live_id") != live["video_id"]
            runtime.update({"live_id": live["video_id"], "chat_id": live["chat_id"], "live_title": live["title"],
                            "status": "Automação ativa — " + channel_title})
            if new_live:
                runtime["seen_users"] = []; runtime["page_token"] = None
                if s.get("startup_enabled") and s.get("startup_message", "").strip():
                    send_chat(yt, live["chat_id"], s["startup_message"])
                    log("Mensagem de início enviada.")
            wait = poll_chat(yt, live["chat_id"]) if s.get("welcome_enabled") else 8
            now = time.time()
            if now >= runtime["next_send"]:
                messages = [m for m in get_messages() if m.get("enabled", True) and m.get("text", "").strip()]
                if messages:
                    state = get_settings()
                    idx = int(state.get("message_cursor", 0)) % len(messages)
                    message = messages[idx]["text"].strip()
                    send_chat(yt, live["chat_id"], message)
                    state["message_cursor"] = idx + 1
                    write_json(STATE_FILE, state)
                    runtime["last_sent"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
                    runtime["next_send"] = now + max(1, int(s.get("interval", 5))) * 60
                    log("Mensagem enviada: " + message[:100])
                elif s.get("link_enabled") and s.get("link_message", "").strip():
                    send_chat(yt, live["chat_id"], s["link_message"])
                    runtime["next_send"] = now + max(1, int(s.get("interval", 5))) * 60
                    log("Mensagem com link enviada.")
            time.sleep(min(max(wait, 5), 15))
        except Exception as e:
            runtime["last_error"] = str(e)
            runtime["status"] = "Erro — verifique conexão e permissões"
            log(str(e), "error")
            time.sleep(15)
    runtime.update({"running": False, "paused": False, "status": "Parado"})
    log("Automação parada.")

def start_worker():
    global worker_thread
    if worker_thread and worker_thread.is_alive():
        runtime["paused"] = False; runtime["status"] = "Automação retomada"
        log("Automação continuada."); return
    worker_stop.clear()
    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()

LOGIN = r'''<!doctype html><html lang="pt-br"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DEUSA PSICO AI</title><style>
*{box-sizing:border-box}body{margin:0;background:#090b12;color:#eef0f8;font:15px Arial;display:grid;place-items:center;min-height:100vh}
.card{background:#141824;border:1px solid #292f40;border-radius:18px;padding:32px;width:min(92vw,390px)}
h1{font-size:23px;margin:0 0 8px}.muted{color:#9aa3b7;margin-bottom:24px}input,button{width:100%;padding:13px;border-radius:10px;border:1px solid #343c50;background:#0d111b;color:white;margin:7px 0}
button{background:#7657e8;border:0;font-weight:bold;cursor:pointer}.err{color:#ff8585}</style>
<div class="card"><h1>DEUSA PSICO AI</h1><div class="muted">Acesso ao painel</div>
{% with msgs=get_flashed_messages() %}{% for m in msgs %}<p class="err">{{m}}</p>{% endfor %}{% endwith %}
<form method="post"><input type="password" name="password" placeholder="Senha do painel" required><button>ENTRAR</button></form></div></html>'''

PAGE = r'''<!doctype html><html lang="pt-br"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DEUSA PSICO AI</title><style>
:root{--bg:#090b12;--panel:#141824;--line:#282f40;--txt:#eef0f8;--muted:#9aa3b7;--purple:#7657e8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:14px Arial}.layout{display:flex;min-height:100vh}
.side{width:235px;background:#0e121c;border-right:1px solid var(--line);padding:25px 17px;flex-shrink:0}.logo{font-size:18px;font-weight:800}.sub{color:var(--muted);font-size:12px;margin:7px 0 25px}
.nav{display:block;color:#aeb6c8;text-decoration:none;padding:12px;border-radius:9px;margin:5px 0}.nav:hover,.nav.active{background:#24203b;color:white}
main{padding:28px;width:100%;max-width:1250px;margin:auto}.top{display:flex;justify-content:space-between;align-items:center;gap:15px;margin-bottom:22px}
h1{font-size:24px;margin:0 0 6px}h2{font-size:16px;margin:0 0 17px}.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:13px;margin-bottom:16px}
.panel,.stat{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:15px}.stat small{color:var(--muted)}.stat strong{display:block;font-size:16px;margin-top:8px;overflow-wrap:anywhere}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.field{margin:12px 0;flex:1;min-width:180px}label{display:block;color:#bac2d2;font-size:12px;margin-bottom:7px}
input,textarea{width:100%;background:#0c111b;border:1px solid #343c50;color:white;border-radius:9px;padding:11px;font:14px Arial}textarea{min-height:70px}
button,.btn{border:0;border-radius:9px;background:var(--purple);color:white;padding:11px 15px;font-weight:700;cursor:pointer;text-decoration:none;display:inline-block}
button.secondary,.btn.secondary{background:#293247}button.danger{background:#873a48}button.green{background:#217b58}
.pill{padding:6px 9px;border-radius:30px;background:#263244;color:#dce4f3;font-size:12px}.good{color:#42c98a}.bad{color:#f06b75}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:middle}th{color:var(--muted);font-size:12px}
.msg{overflow-wrap:anywhere;max-width:430px}.actions{display:flex;gap:7px;flex-wrap:wrap}.small{font-size:12px}.logs{max-height:260px;overflow:auto}.log{padding:9px;border-bottom:1px solid var(--line);font-size:12px}.log.error{color:#ff929a}
.flash{padding:10px;background:#25203b;border:1px solid #51418a;border-radius:9px;margin-bottom:12px}
@media(max-width:760px){.layout{display:block}.side{width:100%;padding:15px}.navs{display:flex;overflow:auto}.nav{white-space:nowrap;margin:2px}.sub{margin-bottom:10px}main{padding:16px}.grid{grid-template-columns:1fr}.top{align-items:flex-start;flex-direction:column}}
</style></head><body><div class="layout"><aside class="side"><div class="logo">DEUSA PSICO AI</div><div class="sub">Painel de automação do YouTube</div><div class="navs">
<a class="nav active" href="#inicio">Visão geral</a><a class="nav" href="#canal">Google e canal</a><a class="nav" href="#mensagens">Mensagens</a><a class="nav" href="#config">Configurações</a><a class="nav" href="#historico">Histórico</a></div><p style="margin-top:28px"><a class="nav" href="/logout">Sair</a></p></aside>
<main><div class="top"><div><h1>Painel de controle</h1><div class="muted">Gerencie mensagens da sua live</div></div><span class="pill">{{runtime.status}}</span></div>
{% with msgs=get_flashed_messages() %}{% for m in msgs %}<div class="flash">{{m}}</div>{% endfor %}{% endwith %}
<section id="inicio" class="grid"><div class="stat"><small>Google</small><strong class="{{'good' if connected else 'bad'}}">{{'Conectado' if connected else 'Não conectado'}}</strong></div>
<div class="stat"><small>Automação</small><strong>{{'Pausada' if runtime.paused else ('Em execução' if runtime.running else 'Parada')}}</strong></div>
<div class="stat"><small>Live</small><strong>{{runtime.live_title or 'Nenhuma live detectada'}}</strong></div></section>
<section class="panel" id="canal"><h2>1. Google e canal do YouTube</h2><div class="row"><div class="field"><label>Conta Google</label>{{'Autorização ativa' if connected else 'Conecte a conta que administra ou modera o canal'}}</div>
<div class="actions">{% if connected %}<a class="btn secondary" href="/google/disconnect">DESCONECTAR GOOGLE</a>{% else %}<a class="btn" href="/google/connect">CONECTAR GOOGLE</a>{% endif %}</div></div>
<form method="post" action="/channel"><div class="field"><label>Link ou identificador do canal (ex.: https://www.youtube.com/@canal)</label><input name="channel" value="{{settings.channel}}" placeholder="@nome, URL ou ID UC..." required></div><div class="actions"><button>SALVAR CANAL</button><button class="secondary" formaction="/check" formmethod="post">TESTAR CONEXÃO E LIVE</button></div></form>
<p class="muted small">A conta autorizada precisa ter permissão para enviar mensagens no chat daquela live.</p></section>
<section class="panel" id="mensagens"><h2>2. Mensagens personalizadas</h2>
<form method="post" action="/upload" enctype="multipart/form-data"><div class="row"><div class="field"><label>Importar TXT — uma mensagem por linha</label><input type="file" name="file" accept=".txt" required></div><button>IMPORTAR TXT</button></div></form>
<form method="post" action="/messages/add"><div class="row"><div class="field"><label>Nova mensagem</label><input name="text" maxlength="200" placeholder="Digite uma mensagem (máximo 200 caracteres)" required></div><button>ADICIONAR</button></div></form>
<div style="overflow:auto"><table><thead><tr><th>#</th><th>Mensagem (edite e salve)</th><th>Ativa</th><th>Ações</th></tr></thead><tbody>
{% for m in messages %}<tr><td>{{loop.index}}</td><td class="msg"><form id="edit{{m.id}}" method="post" action="/messages/edit/{{m.id}}"><input name="text" value="{{m.text}}" maxlength="200"></form></td><td>{{'Sim' if m.enabled else 'Não'}}</td><td><div class="actions"><button form="edit{{m.id}}" class="secondary">SALVAR</button><form method="post" action="/messages/toggle/{{m.id}}"><button class="green">{{'DESATIVAR' if m.enabled else 'ATIVAR'}}</button></form><form method="post" action="/messages/delete/{{m.id}}" onsubmit="return confirm('Excluir mensagem?')"><button class="danger">EXCLUIR</button></form></div></td></tr>{% else %}<tr><td colspan="4" class="muted">Nenhuma mensagem. Importe um TXT ou adicione uma.</td></tr>{% endfor %}</tbody></table></div>
<p class="muted small">Mensagens enviadas em rodízio na ordem cadastrada. Intervalo mínimo de 1 minuto.</p></section>
<section class="panel" id="config"><h2>3. Configurações</h2><form method="post" action="/settings">
<div class="field"><label>Intervalo entre mensagens (minutos, mínimo 1)</label><input type="number" name="interval" min="1" max="180" value="{{settings.interval}}"></div>
<div class="field"><label><input style="width:auto" type="checkbox" name="startup_enabled" {{'checked' if settings.startup_enabled else ''}}> Mensagem quando uma nova live for detectada</label><textarea name="startup_message">{{settings.startup_message}}</textarea></div>
<div class="field"><label><input style="width:auto" type="checkbox" name="welcome_enabled" {{'checked' if settings.welcome_enabled else ''}}> Boas-vindas a novos participantes</label><textarea name="welcome_message">{{settings.welcome_message}}</textarea><div class="muted small">Use {nome} para inserir o nome público.</div></div>
<div class="field"><label><input style="width:auto" type="checkbox" name="link_enabled" {{'checked' if settings.link_enabled else ''}}> Mensagem com link (usada quando não houver mensagens ativas)</label><input name="link_message" value="{{settings.link_message}}" maxlength="200" placeholder="Mensagem/link"></div><button>SALVAR CONFIGURAÇÕES</button></form></section>
<section class="panel"><h2>4. Controles</h2><div class="actions">
<form method="post" action="/start"><button class="green">INICIAR</button></form><form method="post" action="/pause"><button class="secondary">PAUSAR</button></form>
<form method="post" action="/start"><button>CONTINUAR</button></form><form method="post" action="/stop"><button class="danger">PARAR</button></form>
<form method="post" action="/check"><button class="secondary">VERIFICAR LIVE</button></form></div>
<p class="muted small">Render Free pode suspender o serviço quando fica inativo; nesse período, tarefas em segundo plano também param.</p></section>
<section class="panel" id="historico"><h2>5. Histórico e erros</h2><div class="logs">{% for item in runtime.logs %}<div class="log {{item.kind}}"><b>{{item.time}}</b> — {{item.message}}</div>{% else %}<div class="muted">Sem registros ainda.</div>{% endfor %}</div>
{% if runtime.last_error %}<p class="bad">Último erro: {{runtime.last_error}}</p>{% endif %}<p class="muted small">Última verificação: {{runtime.last_check or '—'}} · Último envio: {{runtime.last_sent or '—'}}</p></section>
</main></div></body></html>'''

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        expected = os.environ.get("PAINEL_SENHA", "")
        if expected and secrets.compare_digest(request.form.get("password", ""), expected):
            session["logged_in"] = True
            return redirect(url_for("home"))
        flash("Senha incorreta ou PAINEL_SENHA não configurada no Render.")
    return render_template_string(LOGIN)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def home():
    return render_template_string(PAGE, connected=bool(load_credentials()), settings=get_settings(),
                                  messages=get_messages(), runtime=runtime)

@app.route("/google/connect")
@login_required
def google_connect():
    try:
        flow = Flow.from_client_config(get_client_config(), scopes=SCOPES, redirect_uri=redirect_uri())
        url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
        session["oauth_state"] = state
        return redirect(url)
    except Exception as e:
        flash("Falha ao iniciar OAuth: " + str(e))
        return redirect(url_for("home"))

@app.route("/oauth2callback")
@login_required
def oauth_callback():
    try:
        flow = Flow.from_client_config(get_client_config(), scopes=SCOPES, state=session.get("oauth_state"),
                                       redirect_uri=redirect_uri())
        flow.fetch_token(authorization_response=request.url)
        save_credentials(flow.credentials)
        log("Conta Google conectada.")
        flash("Google conectado com sucesso!")
    except Exception as e:
        log("Erro no retorno OAuth: " + str(e), "error")
        flash("Falha ao conectar Google: " + str(e))
    return redirect(url_for("home"))

@app.route("/google/disconnect")
@login_required
def google_disconnect():
    global creds
    creds = None
    try:
        if os.path.exists(TOKEN_FILE): os.remove(TOKEN_FILE)
    except OSError: pass
    log("Google desconectado.")
    flash("Google desconectado.")
    return redirect(url_for("home"))

@app.route("/channel", methods=["POST"])
@login_required
def channel():
    s = get_settings(); s["channel"] = request.form.get("channel", "").strip(); write_json(STATE_FILE, s)
    flash("Canal salvo."); return redirect(url_for("home"))

@app.route("/settings", methods=["POST"])
@login_required
def settings():
    s = get_settings()
    s["interval"] = max(1, min(180, int(request.form.get("interval", 5) or 5)))
    for key in ("startup_enabled", "welcome_enabled", "link_enabled"): s[key] = key in request.form
    for key in ("startup_message", "welcome_message", "link_message"): s[key] = request.form.get(key, "")[:200]
    write_json(STATE_FILE, s); flash("Configurações salvas."); return redirect(url_for("home"))

@app.route("/upload", methods=["POST"])
@login_required
def upload():
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".txt"):
        flash("Envie um arquivo .txt."); return redirect(url_for("home"))
    try: content = f.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("Salve o TXT em UTF-8."); return redirect(url_for("home"))
    items = get_messages()
    for line in content.splitlines():
        if line.strip(): items.append({"id": secrets.token_hex(6), "text": line.strip()[:200], "enabled": True})
    write_json(MESSAGES_FILE, items); flash("TXT importado; mensagens adicionadas ao final.")
    return redirect(url_for("home"))

@app.route("/messages/add", methods=["POST"])
@login_required
def add_message():
    value = request.form.get("text", "").strip()
    if value:
        items = get_messages(); items.append({"id": secrets.token_hex(6), "text": value[:200], "enabled": True})
        write_json(MESSAGES_FILE, items)
    return redirect(url_for("home"))

@app.route("/messages/edit/<mid>", methods=["POST"])
@login_required
def edit_message(mid):
    items = get_messages()
    for item in items:
        if item["id"] == mid: item["text"] = request.form.get("text", "").strip()[:200]
    write_json(MESSAGES_FILE, items); return redirect(url_for("home"))

@app.route("/messages/toggle/<mid>", methods=["POST"])
@login_required
def toggle_message(mid):
    items = get_messages()
    for item in items:
        if item["id"] == mid: item["enabled"] = not item.get("enabled", True)
    write_json(MESSAGES_FILE, items); return redirect(url_for("home"))

@app.route("/messages/delete/<mid>", methods=["POST"])
@login_required
def delete_message(mid):
    write_json(MESSAGES_FILE, [m for m in get_messages() if m["id"] != mid])
    return redirect(url_for("home"))

@app.route("/check", methods=["POST"])
@login_required
def check():
    try:
        result = check_live()
        flash("Conexão OK! Live: " + result["title"] if result["live"] else
              "Google e canal verificados; nenhuma live com chat ativo agora.")
    except Exception as e:
        log("Teste de conexão: " + str(e), "error"); flash("Falha no teste: " + str(e))
    return redirect(url_for("home"))

@app.route("/start", methods=["POST"])
@login_required
def start():
    if not load_credentials(): flash("Conecte o Google antes de iniciar.")
    elif not get_settings().get("channel"): flash("Informe e salve o canal antes de iniciar.")
    else: start_worker(); flash("Automação iniciada/continuada.")
    return redirect(url_for("home"))

@app.route("/pause", methods=["POST"])
@login_required
def pause():
    runtime["paused"] = True; runtime["status"] = "Pausado"; log("Automação pausada.")
    return redirect(url_for("home"))

@app.route("/stop", methods=["POST"])
@login_required
def stop():
    worker_stop.set(); runtime["status"] = "Parando"
    return redirect(url_for("home"))

@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "DEUSA PSICO AI"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
