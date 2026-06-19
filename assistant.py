#!/usr/bin/env python3
"""Личный ИИ-ассистент на DeepSeek-V3 (через бесплатные GitHub Models).

Терминальный агент «как Claude Code»: общается, читает и правит файлы,
выполняет команды на твоём ПК, решает задачи по шагам. Есть PLAN MODE
(только чтение + план, без изменений) — включается командой /plan.

Мозг — в облаке (GitHub Models), твой ПК не считает модель. Бесплатно по
твоему GitHub-токену; при дневном лимите одной модели — авто-переход на следующую.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import pathlib
import platform
import re
import subprocess
import sys

import requests

# Провайдеры (OpenAI-совместимые). У каждого свой бесплатный лимит; ключ берётся
# из перечисленных переменных окружения (github — из токена, см. load_token).
PROVIDERS = {
    "github":     {"base": "https://models.github.ai/inference", "keys": ["GITHUB_TOKEN", "LLM_API_KEY"]},
    "groq":       {"base": "https://api.groq.com/openai/v1",      "keys": ["GROQ_API_KEY"]},
    "openrouter": {"base": "https://openrouter.ai/api/v1",        "keys": ["OPENROUTER_API_KEY"]},
    "gemini":     {"base": "https://generativelanguage.googleapis.com/v1beta/openai", "keys": ["GEMINI_API_KEY", "GOOGLE_API_KEY"]},
    "ollama":     {"base": "http://localhost:11434/v1",           "keys": []},
}

# Цепочка «сильнее → запаснее». Упёрся лимит/ошибка/нет ключа — берём следующую.
# Все модели проверены на поддержку инструментов. У каждой GitHub-модели СВОЙ
# суточный лимит, токен один — поэтому запас большой и бесплатный.
CHAIN = [
    ("github", "deepseek/deepseek-v3-0324"),
    ("github", "openai/gpt-4o"),
    ("github", "openai/gpt-4.1"),
    ("github", "mistral-ai/mistral-medium-2505"),
    ("github", "cohere/cohere-command-a"),
    ("github", "meta/llama-3.3-70b-instruct"),
    ("github", "openai/gpt-4.1-mini"),
    ("github", "openai/gpt-4o-mini"),
    # ↓ включатся автоматически, когда добавишь бесплатный ключ в .env
    ("groq", "llama-3.3-70b-versatile"),
    ("openrouter", "deepseek/deepseek-chat-v3-0324:free"),
    ("gemini", "gemini-2.0-flash"),
    # ↓ безлимитный локальный этаж, если установлен Ollama
    ("ollama", "qwen2.5-coder:7b"),
]
# Модели, которые УМЕЮТ смотреть картинки (для инструмента view_image).
VISION_CHAIN = [
    ("github", "openai/gpt-4o"),
    ("github", "openai/gpt-4.1"),
    ("github", "openai/gpt-4o-mini"),
    ("openrouter", "google/gemini-2.0-flash-exp:free"),
]
MAX_TOOL_STEPS = 30          # предохранитель от зацикливания за один ответ
MAX_FILE_BYTES = 200_000     # лимит чтения файла
ROOT = pathlib.Path(__file__).resolve().parent

C = {  # цвета терминала
    "g": "\033[92m", "y": "\033[93m", "c": "\033[96m",
    "r": "\033[91m", "d": "\033[2m", "b": "\033[1m", "x": "\033[0m",
}


def _setup_console() -> None:
    """UTF-8 вывод + цвет в консоли. На Windows включаем поддержку ANSI (VT);
    если не получилось — отключаем цвета, чтобы коды не сыпались мусором."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if os.name != "nt":
        return  # на Linux/Mac цвета и UTF-8 уже работают
    color_ok = False
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleOutputCP(65001)   # UTF-8, чтобы кириллица не ломалась
        k.SetConsoleCP(65001)
        ENABLE_VT = 0x0004            # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        for handle_id in (-11, -12):  # stdout, stderr
            h = k.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)) and \
               k.SetConsoleMode(h, mode.value | ENABLE_VT):
                color_ok = True
    except Exception:
        color_ok = False
    if not color_ok:                  # консоль не умеет ANSI — убираем цвета
        for key in list(C):
            C[key] = ""


# --------------------------------------------------------------------------- #
# Память и сессии (хранятся в ~/.deepseek-assistant)
# --------------------------------------------------------------------------- #
DATA_DIR = pathlib.Path.home() / ".deepseek-assistant"
SESSIONS_DIR = DATA_DIR / "sessions"
MEMORY_FILE = DATA_DIR / "memory.md"


def _ensure_dirs() -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def load_memory() -> str:
    try:
        return MEMORY_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def add_memory(text: str) -> str:
    line = text.strip()
    if not line:
        return ""
    _ensure_dirs()
    with MEMORY_FILE.open("a", encoding="utf-8") as f:
        f.write("- " + line + "\n")
    return line


def clear_memory() -> None:
    try:
        MEMORY_FILE.unlink()
    except FileNotFoundError:
        pass


def _now_id() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _now_human() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def save_session(state: dict, messages: list[dict]) -> None:
    if not messages:
        return
    _ensure_dirs()
    data = {
        "id": state["session_id"], "created": state.get("created"),
        "updated": _now_human(), "title": state.get("title", ""),
        "messages": messages,
    }
    try:
        state["session_path"].write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def list_sessions() -> list[dict]:
    _ensure_dirs()
    out: list[dict] = []
    for p in SESSIONS_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append({"path": p, "id": d.get("id", p.stem),
                        "updated": d.get("updated", ""),
                        "title": d.get("title", ""),
                        "n": len(d.get("messages", []))})
        except Exception:
            continue
    out.sort(key=lambda x: x["updated"], reverse=True)
    return out


def load_session(path) -> dict:
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Токен
# --------------------------------------------------------------------------- #
def load_token() -> str:
    """GitHub-токен из env или из .env рядом со скриптом."""
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("LLM_API_KEY")
    if not tok:
        env = ROOT / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("GITHUB_TOKEN="):
                    tok = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not tok:
        sys.exit("Нет GITHUB_TOKEN. Добавь его в .env (см. .env.example).")
    return tok


TOKEN = ""  # заполняется в main()


# --------------------------------------------------------------------------- #
# Вызов модели с авто-переключением
# --------------------------------------------------------------------------- #
def short(model: str) -> str:
    return model.split("/")[-1]


def env_context() -> str:
    """Реальные сведения о ПК пользователя — чтобы модель не гадала пути/ОС."""
    home = pathlib.Path.home()
    win = os.name == "nt"
    return (
        "ОКРУЖЕНИЕ (используй эти РЕАЛЬНЫЕ данные, не выдумывай пути):\n"
        f"- ОС: {platform.system()} {platform.release()}\n"
        f"- Домашняя папка: {home}\n"
        f"- Рабочий стол: {home / 'Desktop'}\n"
        f"- Текущая папка (откуда запущен ассистент): {pathlib.Path.cwd()}\n"
        f"- Shell для run_shell: {'cmd.exe (Windows)' if win else 'bash'}\n"
        "ПРАВИЛА РАБОТЫ С ПК:\n"
        + ("- Windows: НЕ используй '~' и Linux-пути; пиши полные пути с обратными "
           "слэшами (C:\\Users\\...). Команды shell — под cmd (mkdir, dir, copy, del, type).\n"
           if win else
           "- Linux/Mac: используй обычные пути и bash-команды.\n")
        + "- Создавать/менять файлы лучше инструментами write_file/edit_file (кроссплатформенно, "
        "сами раскрывают пути), а не 'echo >' в shell.\n"
        "- Не уверен в пути/наличии файла — сначала проверь list_dir или read_file, потом действуй."
    )


def _provider_key(provider: str) -> str:
    """Ключ провайдера из env; для github — пользовательский токен."""
    for env_name in PROVIDERS[provider]["keys"]:
        v = os.environ.get(env_name)
        if v:
            return v
    if provider == "github":
        return TOKEN
    return ""


def chat(messages: list[dict], tools: list[dict] | None, start_idx: int = 0):
    """Идём по цепочке провайдеров/моделей с start_idx; при лимите/ошибке/без
    ключа — следующая. Возвращает (сообщение, индекс сработавшей записи)."""
    last = ""
    for idx in range(start_idx, len(CHAIN)):
        provider, model = CHAIN[idx]
        key = _provider_key(provider)
        if provider != "ollama" and not key:
            continue  # нет ключа у провайдера — просто пропускаем
        payload: dict = {"model": model, "messages": messages, "temperature": 0.4}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            r = requests.post(PROVIDERS[provider]["base"].rstrip("/") + "/chat/completions",
                              headers=headers, json=payload, timeout=180)
        except requests.RequestException as e:
            last = f"{provider}: сеть {e}"
            continue
        if r.status_code == 200:
            try:
                return r.json()["choices"][0]["message"], idx
            except Exception:
                last = f"{short(model)}: кривой ответ"
                continue
        last = f"{short(model)} HTTP {r.status_code}"
    raise RuntimeError(f"все модели в цепочке недоступны ({last})")


def chat_vision(image_url: str, question: str) -> str:
    """Спросить vision-модель про картинку (image_url — http(s) или data:base64)."""
    content = [
        {"type": "text", "text": question or "Опиши подробно, что на изображении."},
        {"type": "image_url", "image_url": {"url": image_url}},
    ]
    messages = [{"role": "user", "content": content}]
    last = ""
    for provider, model in VISION_CHAIN:
        key = _provider_key(provider)
        if provider != "ollama" and not key:
            continue
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            r = requests.post(PROVIDERS[provider]["base"].rstrip("/") + "/chat/completions",
                              headers=headers,
                              json={"model": model, "messages": messages, "temperature": 0.2},
                              timeout=90)
        except requests.RequestException as e:
            last = f"{provider}: сеть {e}"
            continue
        if r.status_code == 200:
            try:
                return r.json()["choices"][0]["message"]["content"] or "(пустой ответ зрения)"
            except Exception:
                last = "кривой ответ"
                continue
        last = f"{short(model)} HTTP {r.status_code}"
    return f"не удалось посмотреть изображение ({last})"


# --------------------------------------------------------------------------- #
# Инструменты (что агент умеет делать с ПК)
# --------------------------------------------------------------------------- #
def t_read_file(path: str) -> str:
    p = pathlib.Path(path).expanduser()
    if not p.exists():
        return f"НЕТ ФАЙЛА: {path}"
    data = p.read_bytes()[:MAX_FILE_BYTES]
    return data.decode("utf-8", "replace")


def t_list_dir(path: str = ".") -> str:
    p = pathlib.Path(path).expanduser()
    if not p.exists():
        return f"НЕТ ПАПКИ: {path}"
    items = []
    for ch in sorted(p.iterdir()):
        items.append(("[dir] " if ch.is_dir() else "      ") + ch.name)
    return "\n".join(items) or "(пусто)"


def t_search(pattern: str, path: str = ".") -> str:
    try:
        out = subprocess.run(
            ["grep", "-rniI", "--line-number", pattern, path],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception as e:
        return f"ошибка поиска: {e}"
    return "\n".join(out.splitlines()[:80]) or "(ничего не найдено)"


def t_write_file(path: str, content: str) -> str:
    p = pathlib.Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"ЗАПИСАНО: {path} ({len(content)} символов)"


def t_edit_file(path: str, old: str, new: str) -> str:
    p = pathlib.Path(path).expanduser()
    if not p.exists():
        return f"НЕТ ФАЙЛА: {path}"
    text = p.read_text(encoding="utf-8")
    if old not in text:
        return "НЕ НАЙДЕН фрагмент old — правка не сделана."
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    return f"ИЗМЕНЁН: {path}"


def t_run_shell(command: str) -> str:
    # Страховка: на Windows '~' не раскрывается — подставим домашнюю папку.
    home = str(pathlib.Path.home())
    command = command.replace("~/", home + os.sep).replace("~\\", home + os.sep)
    try:
        r = subprocess.run(command, shell=True, capture_output=True,
                           text=True, timeout=300)
    except Exception as e:
        return f"ошибка запуска: {e}"
    out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    return f"exit={r.returncode}\n{out[:8000]}"


def t_remember(fact: str) -> str:
    saved = add_memory(fact)
    return f"ЗАПОМНЕНО: {saved}" if saved else "пустой факт — не сохранил"


_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}


def t_view_image(source: str, question: str = "") -> str:
    """Глаза: посмотреть картинку (локальный файл или http(s)-ссылка) и описать.

    Картинку всегда кодируем в base64 и шлём инлайн (надёжнее, чем просить модель
    самой скачать URL)."""
    src = source.strip()
    if src.lower().startswith(("http://", "https://")):
        try:
            r = requests.get(src, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            if r.status_code != 200:
                return f"не скачать картинку (HTTP {r.status_code})"
            raw = r.content
            mime = r.headers.get("Content-Type", "").split(";")[0] or "image/jpeg"
            if not mime.startswith("image/"):
                mime = _MIME.get(pathlib.Path(src.split("?")[0]).suffix.lower(), "image/jpeg")
        except Exception as e:
            return f"ошибка загрузки картинки: {e}"
    else:
        p = pathlib.Path(src).expanduser()
        if not p.exists():
            return f"НЕТ ФАЙЛА: {source}"
        raw = p.read_bytes()
        mime = _MIME.get(p.suffix.lower(), "image/png")
    b64 = base64.b64encode(raw).decode("ascii")
    return chat_vision(f"data:{mime};base64,{b64}", question)


def t_search_photos(query: str, count: int = 5) -> str:
    """Поиск фото на бесплатном стоке Pexels (нужен бесплатный PEXELS_API_KEY)."""
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        return ("нет PEXELS_API_KEY. Заведи бесплатный ключ на https://www.pexels.com/api/ "
                "и добавь PEXELS_API_KEY=... в .env. (Анализ конкретной картинки/ссылки работает "
                "и без ключа — через view_image.)")
    try:
        r = requests.get("https://api.pexels.com/v1/search",
                         headers={"Authorization": key},
                         params={"query": query, "per_page": max(1, min(int(count), 15))},
                         timeout=30)
        if r.status_code != 200:
            return f"Pexels HTTP {r.status_code}: {r.text[:200]}"
        photos = r.json().get("photos", [])
    except Exception as e:
        return f"ошибка Pexels: {e}"
    if not photos:
        return "ничего не найдено"
    out = []
    for p in photos:
        out.append(f"- {p['src'].get('large') or p['src'].get('original')}  "
                   f"(автор: {p.get('photographer','?')}; {p.get('alt','') or 'без описания'})")
    return "Фото (ссылки можно открыть/скачать/посмотреть через view_image):\n" + "\n".join(out)


def t_web_search(query: str) -> str:
    """Веб-поиск без ключа (DuckDuckGo). Топ результатов: заголовок, ссылка, сниппет."""
    try:
        r = requests.post("https://html.duckduckgo.com/html/", data={"q": query},
                          headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        html = r.text
    except Exception as e:
        return f"ошибка поиска: {e}"
    items = re.findall(r'result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?result__snippet"[^>]*>(.*?)</a>',
                       html, re.S)
    if not items:
        items = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)
        items = [(u, t, "") for u, t in items]
    def clean(s: str) -> str:
        s = re.sub(r"<[^>]+>", "", s)
        return re.sub(r"\s+", " ", s).strip()
    if not items:
        return "ничего не найдено (или поиск временно недоступен)"
    out = []
    for u, t, sn in items[:6]:
        link = re.sub(r"^.*?uddg=", "", u)
        link = requests.utils.unquote(link.split("&")[0]) if "uddg=" in u else u
        out.append(f"- {clean(t)}\n  {link}\n  {clean(sn)}")
    return "\n".join(out)


def t_fetch_url(url: str) -> str:
    """Скачать веб-страницу и вернуть читаемый текст (без скриптов и тегов)."""
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        html = r.text
    except Exception as e:
        return f"ошибка загрузки: {e}"
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"&[a-z#0-9]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:8000] or "(пустая страница)"


def t_download_file(url: str, path: str) -> str:
    """Скачать файл (фото/документ) по ссылке на диск."""
    p = pathlib.Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
        p.write_bytes(r.content)
    except Exception as e:
        return f"ошибка скачивания: {e}"
    return f"СКАЧАНО: {path} ({len(r.content)} байт)"


def t_run_python(code: str) -> str:
    """Выполнить фрагмент Python и вернуть вывод (для расчётов/обработки данных)."""
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=60)
    except Exception as e:
        return f"ошибка запуска: {e}"
    out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
    return out[:8000] or "(нет вывода)"


def t_open_path(target: str) -> str:
    """Открыть файл/папку/ссылку в системном приложении по умолчанию."""
    try:
        if os.name == "nt":
            os.startfile(target)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
    except Exception as e:
        return f"не удалось открыть: {e}"
    return f"ОТКРЫТО: {target}"


TOOLS_IMPL = {
    "read_file": t_read_file, "list_dir": t_list_dir, "search": t_search,
    "write_file": t_write_file, "edit_file": t_edit_file, "run_shell": t_run_shell,
    "remember": t_remember, "view_image": t_view_image, "search_photos": t_search_photos,
    "web_search": t_web_search, "fetch_url": t_fetch_url, "download_file": t_download_file,
    "run_python": t_run_python, "open_path": t_open_path,
}
# Безопасные инструменты — доступны и в PLAN MODE, без подтверждения.
READONLY = {"read_file", "list_dir", "search", "remember",
            "view_image", "search_photos", "web_search", "fetch_url"}
MUTATING = {"write_file", "edit_file", "run_shell",
            "download_file", "run_python", "open_path"}

TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "read_file", "description": "Прочитать содержимое файла.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "list_dir", "description": "Список файлов в папке.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "search", "description": "Поиск текста по файлам (grep).",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string"}},
            "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Создать/перезаписать файл.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file", "description": "Заменить фрагмent old на new в файле.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old": {"type": "string"},
            "new": {"type": "string"}}, "required": ["path", "old", "new"]}}},
    {"type": "function", "function": {
        "name": "run_shell", "description": "Выполнить команду в shell на ПК.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "remember",
        "description": "Сохранить устойчивый факт о пользователе/его предпочтениях в долгую "
                       "память (доступна во всех будущих разговорах). Только важное, не болтовню.",
        "parameters": {"type": "object", "properties": {
            "fact": {"type": "string"}}, "required": ["fact"]}}},
    {"type": "function", "function": {
        "name": "view_image",
        "description": "ГЛАЗА: посмотреть изображение (локальный файл или http(s)-ссылка) и "
                       "ответить, что на нём, оценить качество, пригодность и т.п.",
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string", "description": "путь к файлу или URL картинки"},
            "question": {"type": "string", "description": "что именно спросить про картинку"}},
            "required": ["source"]}}},
    {"type": "function", "function": {
        "name": "search_photos",
        "description": "Найти фото на бесплатном стоке Pexels по запросу — вернёт ссылки на фото "
                       "(их можно посмотреть через view_image или скачать через download_file).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "count": {"type": "integer"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Поиск в интернете (актуальная информация, новости, факты). Возвращает "
                       "заголовки, ссылки и сниппеты.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "fetch_url",
        "description": "Открыть веб-страницу по ссылке и вернуть её текст (для чтения статьи/доки).",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "download_file",
        "description": "Скачать файл (фото, документ) по ссылке на диск пользователя.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}, "path": {"type": "string"}},
            "required": ["url", "path"]}}},
    {"type": "function", "function": {
        "name": "run_python",
        "description": "Выполнить фрагмент кода Python и вернуть вывод (расчёты, обработка данных).",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string"}}, "required": ["code"]}}},
    {"type": "function", "function": {
        "name": "open_path",
        "description": "Открыть файл, папку или ссылку в системном приложении по умолчанию.",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string"}}, "required": ["target"]}}},
]


def tools_for(plan_mode: bool) -> list[dict]:
    """В plan mode отдаём модели только инструменты чтения."""
    if not plan_mode:
        return TOOLS_SCHEMA
    return [t for t in TOOLS_SCHEMA if t["function"]["name"] in READONLY]


# --------------------------------------------------------------------------- #
# Выполнение инструмента (с защитой)
# --------------------------------------------------------------------------- #
def confirm(name: str, args: dict, state: dict) -> bool:
    """Подтверждение мутирующих действий вне plan mode."""
    if state["yolo"]:
        return True
    preview = (args.get("command") or args.get("path") or args.get("url")
               or args.get("target") or args.get("code") or "")
    print(f"{C['y']}  ⚙ {name}: {str(preview)[:120]}{C['x']}")
    try:
        ans = input(f"{C['y']}  Выполнить? [y/N/a=всегда] {C['x']}").strip().lower()
    except EOFError:
        return False
    if ans in ("a", "always", "всегда"):
        state["yolo"] = True
        return True
    return ans in ("y", "yes", "да", "д")


SYSTEM = """Ты — личный ИИ-ассистент в терминале на ПК пользователя (Windows + WSL/Linux). \
Общайся по-русски, кратко и по делу. Ты умеешь РЕАЛЬНО работать с компьютером через инструменты.

Думай по шагам: пойми задачу → собери факты нужными инструментами → действуй → ПРОВЕРЬ результат. \
Не выдумывай содержимое файлов, пути и факты — узнавай их инструментами.

Твои инструменты:
- Файлы/ПК: read_file, list_dir, search, write_file, edit_file, run_shell, open_path, run_python.
- ГЛАЗА: view_image — посмотреть картинку (локальный файл ИЛИ ссылка) и описать/оценить.
- Фото-стоки: search_photos — найти бесплатные фото (Pexels), потом можно посмотреть их view_image \
или скачать download_file.
- Интернет: web_search (актуальная инфа, факты, новости) и fetch_url (прочитать страницу).
- Память: remember — сохранить устойчивый факт о пользователе.

ВСЕГДА проверяй свою работу (самоконтроль): создал/изменил файл — перечитай его (read_file); \
запустил код — посмотри вывод; сделал картинку/скачал — при необходимости глянь view_image. \
Не говори «готово», пока не убедился. Заметил ошибку — исправь сам.

Если включён PLAN MODE — доступны ТОЛЬКО инструменты чтения. Выдай план по пунктам, ничего не \
меняя и не запуская. Менять будешь после выхода из plan mode.

ПАМЯТЬ: если пользователь просит запомнить о себе или сообщает устойчивый факт/предпочтение \
(имя, проект, как любит работать) — вызови remember. Не сохраняй мелочи. Что уже помнишь — придёт \
в системном сообщении.

ВЫВОД: простой текст для терминала, без markdown (** , #, таблиц). Списки — строками («1. …»). \
Код — как есть, отдельными строками."""


# --------------------------------------------------------------------------- #
# Один ход диалога: гоняем модель + инструменты до финального ответа
# --------------------------------------------------------------------------- #
def agent_turn(messages: list[dict], state: dict) -> None:
    state["mi"] = 0  # каждый новый запрос пробуем сильнейшую модель сначала
    env = env_context()
    mem = load_memory()
    mem_block = f"\n\nЧто ты помнишь о пользователе (из прошлых разговоров):\n{mem}" if mem else ""
    for _ in range(MAX_TOOL_STEPS):
        sys_note = (SYSTEM + "\n\n" + env + mem_block
                    + ("\n\n[СЕЙЧАС ВКЛЮЧЁН PLAN MODE]" if state["plan"] else ""))
        full = [{"role": "system", "content": sys_note}] + messages
        msg, idx = chat(full, tools_for(state["plan"]), state["mi"])
        state["mi"] = idx  # внутри хода держимся сработавшей модели
        model = CHAIN[idx][1]
        if model != state.get("active"):
            if state.get("active") is not None and idx > 0:
                print(f"{C['y']}⚠ {short(CHAIN[0][1])} занят (лимит) → перешёл на {short(model)}{C['x']}")
            state["active"] = model
        messages.append(msg)

        calls = msg.get("tool_calls")
        if not calls:
            content = msg.get("content") or "(пусто)"
            print(f"\n{C['c']}{content}{C['x']}")
            print(f"{C['d']}— {short(model)}{C['x']}\n")
            return

        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            # защита: plan mode / подтверждение мутаций
            if state["plan"] and name in MUTATING:
                result = "PLAN MODE: изменения запрещены — предложи план."
            elif name in MUTATING and not confirm(name, args, state):
                result = "Отклонено пользователем."
            else:
                preview = (args.get("command") or args.get("path") or args.get("pattern")
                           or args.get("url") or args.get("source") or args.get("query")
                           or args.get("target") or "")
                print(f"{C['d']}  → {name} {str(preview)[:100]}{C['x']}")
                try:
                    result = TOOLS_IMPL[name](**args)
                except Exception as e:
                    result = f"ошибка {name}: {e}"
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": str(result)[:12000]})
    print(f"{C['r']}Прервано: слишком много шагов за один ответ.{C['x']}")


# --------------------------------------------------------------------------- #
# REPL
# --------------------------------------------------------------------------- #
HELP = f"""{C['b']}Управление — просто набери  /  (откроется меню по цифрам).{C['x']}
В меню: 1 план-режим · 2 авто-«да» · 3 память · 4 разговоры · 5 модели · 0 выход.

Просто пиши задачу обычными словами — ассистент сам:
  • читает и правит файлы, выполняет команды;
  • СМОТРИТ картинки (view_image) — файл или ссылку;
  • ищет фото на бесплатных стоках (search_photos) и качает их;
  • ищет в интернете (web_search) и читает страницы (fetch_url);
  • запускает Python, открывает файлы/папки.
Скажи «запомни, что…» — сохранит навсегда. Разговоры сохраняются сами.

{C['d']}Быстрые команды (для опытных): /plan /run /yolo /model /remember /memory
/forget /sessions /resume /new /reset /exit{C['x']}"""


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def show_menu(state: dict, messages: list[dict]) -> str:
    """Меню по цифрам. Возвращает 'exit' для выхода, иначе ''."""
    plan = "ВКЛ" if state["plan"] else "выкл"
    yolo = "ВКЛ" if state["yolo"] else "выкл"
    print(f"{C['b']}МЕНЮ{C['x']} (цифра — действие; просто текст — задача):")
    print(f"  1  План-режим: {plan}   (только чтение+план)")
    print(f"  2  Авто-«да» на действия: {yolo}")
    print("  3  Память (показать/очистить/добавить)")
    print("  4  Разговоры (продолжить/новый/очистить)")
    print("  5  Модели")
    print("  0  Выход")
    c = _ask(f"{C['g']}выбор ›{C['x']} ")

    if c == "1":
        state["plan"] = not state["plan"]
        print(f"{C['y']}План-режим: {'ВКЛ — только чтение и план' if state['plan'] else 'выкл — действия разрешены'}{C['x']}")
    elif c == "2":
        state["yolo"] = not state["yolo"]
        print(f"Авто-«да»: {'ВКЛ' if state['yolo'] else 'выкл'}")
    elif c == "3":
        print("  1 показать   2 очистить   3 добавить факт")
        s = _ask(f"{C['g']}память ›{C['x']} ")
        if s == "1":
            mem = load_memory(); print(mem if mem else "Память пуста.")
        elif s == "2":
            if _ask("Стереть всю память? [y/N] ").lower() in ("y", "да", "д"):
                clear_memory(); print("Память очищена.")
        elif s == "3":
            f = _ask("Что запомнить: ")
            saved = add_memory(f)
            print(f"{C['g']}Запомнил: {saved}{C['x']}" if saved else "пусто.")
    elif c == "4":
        sess = list_sessions()
        for i, s in enumerate(sess[:15], 1):
            print(f"  {i}. {s['updated']}  ({s['n']} сообщ.)  {s['title'] or '(без названия)'}")
        print("  цифра — продолжить разговор · н — новый · о — очистить текущий")
        s = _ask(f"{C['g']}разговоры ›{C['x']} ")
        if s.isdigit() and 1 <= int(s) <= len(sess):
            d = load_session(sess[int(s) - 1]["path"])
            messages[:] = d.get("messages", [])
            state["session_id"] = d.get("id", sess[int(s) - 1]["id"])
            state["session_path"] = sess[int(s) - 1]["path"]
            state["title"] = d.get("title", "")
            state["created"] = d.get("created") or _now_human()
            print(f"{C['g']}Продолжаю: {state['title'] or '(без названия)'} ({len(messages)} сообщ.){C['x']}")
        elif s in ("н", "n", "новый"):
            messages.clear()
            sid2 = _now_id()
            state.update(session_id=sid2, session_path=SESSIONS_DIR / f"{sid2}.json",
                         created=_now_human(), title="")
            print("Новый разговор начат.")
        elif s in ("о", "o"):
            messages.clear(); print("Текущий разговор очищен.")
    elif c == "5":
        print("Цепочка моделей (авто-переключение при лимите):")
        for i, (prov, m) in enumerate(CHAIN, 1):
            star = "  ← сейчас" if m == state["active"] else ""
            avail = "" if (prov == "ollama" or _provider_key(prov)) else "  (нужен ключ)"
            print(f"  {i}. {short(m)} [{prov}]{avail}{star}")
    elif c in ("0", "выход", "exit"):
        return "exit"
    return ""


def main() -> int:
    global TOKEN
    _setup_console()
    TOKEN = load_token()
    sid = _now_id()
    state = {"plan": False, "yolo": False, "active": None, "mi": 0,
             "session_id": sid, "created": _now_human(), "title": "",
             "session_path": SESSIONS_DIR / f"{sid}.json"}
    messages: list[dict] = []

    print(f"{C['g']}{C['b']}DeepSeek ассистент{C['x']} "
          f"{C['d']}(бесплатно). Набери {C['x']}{C['b']}/{C['x']}{C['d']} — меню. Просто пиши задачу.{C['x']}")
    print(f"{C['d']}Умею: файлы, команды, интернет, ЗРЕНИЕ (картинки), фото-стоки. Рабочая папка: {pathlib.Path.cwd()}{C['x']}")
    _past = list_sessions()
    if _past:
        print(f"{C['d']}Прошлых разговоров: {len(_past)}. "
              f"Продолжить последний — /resume, список — /sessions{C['x']}")
    if load_memory():
        print(f"{C['d']}Память загружена (/memory — посмотреть).{C['x']}")
    print()

    while True:
        mode = f"{C['y']}[PLAN]{C['x']} " if state["plan"] else ""
        cur = f"{C['d']}[{short(state['active'])}]{C['x']} " if state["active"] else ""
        try:
            user = input(f"{mode}{cur}{C['g']}ты ›{C['x']} ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nпока 👋")
            return 0
        if not user:
            continue
        if user in ("/", "меню", "menu", "?"):
            if show_menu(state, messages) == "exit":
                save_session(state, messages); print("пока 👋"); return 0
            continue
        if user in ("/exit", "/quit", "выход"):
            save_session(state, messages)
            print("пока 👋")
            return 0
        if user == "/help":
            print(HELP); continue
        if user == "/plan":
            state["plan"] = True
            print(f"{C['y']}PLAN MODE включён — только чтение и план.{C['x']}"); continue
        if user in ("/run", "/exec"):
            state["plan"] = False
            print(f"{C['g']}PLAN MODE выключен — действия разрешены.{C['x']}"); continue
        if user == "/yolo":
            state["yolo"] = not state["yolo"]
            print(f"авто-подтверждение: {'ВКЛ' if state['yolo'] else 'выкл'}"); continue
        if user == "/model":
            print("Цепочка моделей (по приоритету, авто-переключение при лимите):")
            for i, (prov, m) in enumerate(CHAIN, 1):
                star = "  ← сейчас" if m == state["active"] else ""
                avail = "" if (prov == "ollama" or _provider_key(prov)) else "  (нужен ключ)"
                print(f"  {i}. {short(m)} [{prov}]{avail}{star}")
            if not state["active"]:
                print(f"{C['d']}(активная определится после первого ответа){C['x']}")
            continue
        if user == "/reset":
            messages.clear(); print("текущий разговор очищен."); continue
        if user.startswith("/remember"):
            fact = user[len("/remember"):].strip()
            saved = add_memory(fact)
            print(f"{C['g']}Запомнил: {saved}{C['x']}" if saved
                  else "после /remember напиши, что запомнить.")
            continue
        if user == "/memory":
            mem = load_memory()
            print(mem if mem else "Память пуста. Добавить: /remember <текст>")
            continue
        if user == "/forget":
            try:
                ans = input("Стереть всю память? [y/N] ").strip().lower()
            except EOFError:
                ans = ""
            if ans in ("y", "yes", "да", "д"):
                clear_memory(); print("Память очищена.")
            else:
                print("Отменено.")
            continue
        if user == "/sessions":
            sess = list_sessions()
            if not sess:
                print("Прошлых разговоров нет."); continue
            for i, s in enumerate(sess[:15], 1):
                print(f"  {i}. {s['updated']}  ({s['n']} сообщ.)  "
                      f"{s['title'] or '(без названия)'}")
            print(f"{C['d']}Продолжить: /resume <номер> (или /resume — последний){C['x']}")
            continue
        if user.startswith("/resume"):
            sess = list_sessions()
            if not sess:
                print("Прошлых разговоров нет."); continue
            parts = user.split()
            idx = 0
            if len(parts) > 1:
                try:
                    idx = int(parts[1]) - 1
                except ValueError:
                    idx = 0
            if idx < 0 or idx >= len(sess):
                print("Нет такого номера (см. /sessions)."); continue
            d = load_session(sess[idx]["path"])
            messages[:] = d.get("messages", [])
            state["session_id"] = d.get("id", sess[idx]["id"])
            state["session_path"] = sess[idx]["path"]
            state["title"] = d.get("title", "")
            state["created"] = d.get("created") or _now_human()
            print(f"{C['g']}Продолжаю разговор: "
                  f"{state['title'] or '(без названия)'} ({len(messages)} сообщ.){C['x']}")
            continue
        if user == "/new":
            messages.clear()
            sid2 = _now_id()
            state["session_id"] = sid2
            state["session_path"] = SESSIONS_DIR / f"{sid2}.json"
            state["created"] = _now_human()
            state["title"] = ""
            print("Новый разговор начат."); continue

        messages.append({"role": "user", "content": user})
        if not state["title"]:
            state["title"] = user[:40]
        try:
            agent_turn(messages, state)
        except Exception as e:
            print(f"{C['r']}Ошибка: {e}{C['x']}")
        save_session(state, messages)


if __name__ == "__main__":
    sys.exit(main())
