#!/usr/bin/env python3
"""Личный ИИ-ассистент на DeepSeek-V3 (через бесплатные GitHub Models).

Терминальный агент «как Claude Code»: общается, читает и правит файлы,
выполняет команды на твоём ПК, решает задачи по шагам. Есть PLAN MODE
(только чтение + план, без изменений) — включается командой /plan.

Мозг — в облаке (GitHub Models), твой ПК не считает модель. Бесплатно по
твоему GitHub-токену; при дневном лимите одной модели — авто-переход на следующую.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import subprocess
import sys

import requests

ENDPOINT = "https://models.github.ai/inference/chat/completions"
# Цепочка моделей: упёрся лимит/ошибка одной — берём следующую. Все умеют tools.
MODELS = ["deepseek/deepseek-v3-0324", "openai/gpt-4o", "openai/gpt-4o-mini"]
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


def chat(messages: list[dict], tools: list[dict] | None, start_idx: int = 0):
    """Запрос к GitHub Models, начиная с модели start_idx; при лимите/ошибке —
    следующая. Возвращает (сообщение, индекс сработавшей модели)."""
    last = ""
    for idx in range(start_idx, len(MODELS)):
        model = MODELS[idx]
        payload: dict = {"model": model, "messages": messages, "temperature": 0.4}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            r = requests.post(
                ENDPOINT,
                headers={"Authorization": f"Bearer {TOKEN}",
                         "Content-Type": "application/json"},
                json=payload, timeout=180,
            )
        except requests.RequestException as e:
            last = f"сеть: {e}"
            continue
        if r.status_code == 200:
            return r.json()["choices"][0]["message"], idx
        last = f"{short(model)} HTTP {r.status_code}"
    raise RuntimeError(f"все модели недоступны ({last})")


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


TOOLS_IMPL = {
    "read_file": t_read_file, "list_dir": t_list_dir, "search": t_search,
    "write_file": t_write_file, "edit_file": t_edit_file, "run_shell": t_run_shell,
    "remember": t_remember,
}
# Безопасные инструменты — доступны и в PLAN MODE, без подтверждения.
READONLY = {"read_file", "list_dir", "search", "remember"}
MUTATING = {"write_file", "edit_file", "run_shell"}

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
    preview = args.get("command") or f"{args.get('path','')}"
    print(f"{C['y']}  ⚙ {name}: {preview}{C['x']}")
    try:
        ans = input(f"{C['y']}  Выполнить? [y/N/a=всегда] {C['x']}").strip().lower()
    except EOFError:
        return False
    if ans in ("a", "always", "всегда"):
        state["yolo"] = True
        return True
    return ans in ("y", "yes", "да", "д")


SYSTEM = """Ты — личный ИИ-ассистент, работающий в терминале на ПК пользователя \
(Windows + WSL/Linux). Общайся по-русски, кратко и по делу. Ты умеешь РЕАЛЬНО \
работать с компьютером через инструменты: читать и править файлы, искать по коду, \
выполнять команды shell. Действуй по шагам: сначала собери факты (read_file/list_dir/\
search), потом меняй. Не выдумывай содержимое файлов — читай их. После выполнения \
задачи дай короткий итог.

Если включён PLAN MODE — тебе доступны ТОЛЬКО инструменты чтения. Изучи задачу и \
выдай понятный план действий по пунктам, НИЧЕГО не меняя и не запуская. Менять будешь \
после того, как пользователь выйдет из plan mode.

ПАМЯТЬ: если пользователь просит что-то запомнить о себе или сообщает устойчивое \
предпочтение/факт (имя, проект, как он любит работать) — вызови инструмент remember. \
Не сохраняй мелочи и разовые реплики. Что ты уже помнишь — придёт в системном сообщении.

ВЫВОД: пиши простым текстом для терминала, без markdown-разметки — не используй ** для
выделения, # для заголовков и markdown-таблицы. Списки — обычными строками («1. …», «- …»).
Код приводи как есть, отдельными строками."""


# --------------------------------------------------------------------------- #
# Один ход диалога: гоняем модель + инструменты до финального ответа
# --------------------------------------------------------------------------- #
def agent_turn(messages: list[dict], state: dict) -> None:
    state["mi"] = 0  # каждый новый запрос пробуем сильнейшую модель сначала
    mem = load_memory()
    mem_block = f"\n\nЧто ты помнишь о пользователе (из прошлых разговоров):\n{mem}" if mem else ""
    for _ in range(MAX_TOOL_STEPS):
        sys_note = SYSTEM + mem_block + ("\n\n[СЕЙЧАС ВКЛЮЧЁН PLAN MODE]" if state["plan"] else "")
        full = [{"role": "system", "content": sys_note}] + messages
        msg, idx = chat(full, tools_for(state["plan"]), state["mi"])
        state["mi"] = idx  # внутри хода держимся сработавшей модели
        model = MODELS[idx]
        if model != state.get("active"):
            if state.get("active") is not None and idx > 0:
                print(f"{C['y']}⚠ {short(MODELS[0])} занят (лимит) → перешёл на {short(model)}{C['x']}")
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
                preview = args.get("command") or args.get("path") or args.get("pattern") or ""
                print(f"{C['d']}  → {name} {preview}{C['x']}")
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
HELP = f"""{C['b']}Команды:{C['x']}
  /plan      — включить PLAN MODE (только чтение + план, без изменений)
  /run       — выключить PLAN MODE (разрешить правки и команды)
  /yolo      — не спрашивать подтверждение на действия (вкл/выкл)
  /model     — показать модели и какая отвечает сейчас
  {C['b']}Память:{C['x']}
  /remember <текст> — запомнить факт о себе навсегда (на все разговоры)
  /memory    — показать, что ассистент помнит
  /forget    — стереть всю память
  {C['b']}Сессии (разговоры):{C['x']}
  /sessions  — список прошлых разговоров
  /resume [№]— продолжить прошлый разговор (без номера — последний)
  /new       — начать новый разговор
  /reset     — очистить текущий разговор
  /help      — это сообщение
  /exit      — выход
Просто пиши задачу — ассистент сам почитает файлы, поправит код, выполнит команды.
Разговоры сохраняются автоматически; память переживает закрытие окна."""


def main() -> int:
    global TOKEN
    _setup_console()
    TOKEN = load_token()
    sid = _now_id()
    state = {"plan": False, "yolo": False, "active": None, "mi": 0,
             "session_id": sid, "created": _now_human(), "title": "",
             "session_path": SESSIONS_DIR / f"{sid}.json"}
    messages: list[dict] = []

    print(f"{C['g']}{C['b']}DeepSeek-V3 ассистент{C['x']} "
          f"{C['d']}(GitHub Models, бесплатно). /help — команды, /exit — выход.{C['x']}")
    print(f"{C['d']}Рабочая папка: {pathlib.Path.cwd()}{C['x']}")
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
        if user in ("/exit", "/quit"):
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
            for i, m in enumerate(MODELS, 1):
                star = "  ← сейчас" if m == state["active"] else ""
                print(f"  {i}. {m}{star}")
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
