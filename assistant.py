#!/usr/bin/env python3
"""Личный ИИ-ассистент на DeepSeek-V3 (через бесплатные GitHub Models).

Терминальный агент «как Claude Code»: общается, читает и правит файлы,
выполняет команды на твоём ПК, решает задачи по шагам. Есть PLAN MODE
(только чтение + план, без изменений) — включается командой /plan.

Мозг — в облаке (GitHub Models), твой ПК не считает модель. Бесплатно по
твоему GitHub-токену; при дневном лимите одной модели — авто-переход на следующую.
"""

from __future__ import annotations

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
def chat(messages: list[dict], tools: list[dict] | None) -> dict:
    """Запрос к GitHub Models; перебираем модели при лимите/ошибке."""
    last = ""
    for model in MODELS:
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
            msg = r.json()["choices"][0]["message"]
            if model != MODELS[0]:
                print(f"{C['d']}(модель: {model}){C['x']}")
            return msg
        last = f"{model} HTTP {r.status_code}: {r.text[:160]}"
        if r.status_code not in (429, 500, 503):
            # не лимит — нет смысла пробовать другие, но всё же продолжим
            pass
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


TOOLS_IMPL = {
    "read_file": t_read_file, "list_dir": t_list_dir, "search": t_search,
    "write_file": t_write_file, "edit_file": t_edit_file, "run_shell": t_run_shell,
}
READONLY = {"read_file", "list_dir", "search"}
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
    ans = input(f"{C['y']}  Выполнить? [y/N/a=всегда] {C['x']}").strip().lower()
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
после того, как пользователь выйдет из plan mode."""


# --------------------------------------------------------------------------- #
# Один ход диалога: гоняем модель + инструменты до финального ответа
# --------------------------------------------------------------------------- #
def agent_turn(messages: list[dict], state: dict) -> None:
    for _ in range(MAX_TOOL_STEPS):
        sys_note = SYSTEM + ("\n\n[СЕЙЧАС ВКЛЮЧЁН PLAN MODE]" if state["plan"] else "")
        full = [{"role": "system", "content": sys_note}] + messages
        msg = chat(full, tools_for(state["plan"]))
        messages.append(msg)

        calls = msg.get("tool_calls")
        if not calls:
            content = msg.get("content") or "(пусто)"
            print(f"\n{C['c']}{content}{C['x']}\n")
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
                short = args.get("command") or args.get("path") or args.get("pattern") or ""
                print(f"{C['d']}  → {name} {short}{C['x']}")
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
  /plan    — включить PLAN MODE (только чтение + план, без изменений)
  /run     — выключить PLAN MODE (разрешить правки и команды)
  /yolo    — не спрашивать подтверждение на действия (вкл/выкл)
  /reset   — очистить историю диалога
  /help    — это сообщение
  /exit    — выход
Просто пиши задачу — ассистент сам почитает файлы, поправит код, выполнит команды."""


def main() -> int:
    global TOKEN
    TOKEN = load_token()
    state = {"plan": False, "yolo": False}
    messages: list[dict] = []

    print(f"{C['g']}{C['b']}DeepSeek-V3 ассистент{C['x']} "
          f"{C['d']}(GitHub Models, бесплатно). /help — команды, /exit — выход.{C['x']}")
    print(f"{C['d']}Рабочая папка: {pathlib.Path.cwd()}{C['x']}\n")

    while True:
        mode = f"{C['y']}[PLAN]{C['x']} " if state["plan"] else ""
        try:
            user = input(f"{mode}{C['g']}ты ›{C['x']} ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nпока 👋")
            return 0
        if not user:
            continue
        if user in ("/exit", "/quit"):
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
        if user == "/reset":
            messages.clear(); print("история очищена."); continue

        messages.append({"role": "user", "content": user})
        try:
            agent_turn(messages, state)
        except Exception as e:
            print(f"{C['r']}Ошибка: {e}{C['x']}")


if __name__ == "__main__":
    sys.exit(main())
