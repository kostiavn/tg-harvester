#!/usr/bin/env python3
"""
TG Harvester — архивирует сообщения из Telegram-чатов в формат, совместимый
с экспортом Telegram Desktop (result.json).

Команды:
    harvester.py auth                  — первичная авторизация (один раз)
    harvester.py discover [SEARCH]     — показать все твои чаты (с фильтром)
    harvester.py harvest               — собрать новые сообщения (для cron/timer)
    harvester.py dump CHAT N           — выгрузить последние N часов одного чата
    harvester.py archive               — упаковать все JSON в tar.gz и в Saved Messages
    harvester.py listen                — слушать команды в Saved Messages (демон)
"""
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    Channel, Chat, Message, MessageEntityBold, MessageEntityCode,
    MessageEntityHashtag, MessageEntityItalic, MessageEntityMention,
    MessageEntityPre, MessageEntityStrike, MessageEntityTextUrl,
    MessageEntityUnderline, MessageEntityUrl, MessageActionTopicCreate,
    PeerChannel, PeerChat, PeerUser, User,
)

# ---------- Пути ----------

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
SESSION_PATH = ROOT / "session" / "harvester"
STATE_PATH = ROOT / "state.json"
DATA_DIR = ROOT / "data"
ARCHIVE_DIR = ROOT / "archive"
LOG_DIR = ROOT / "logs"

for d in (SESSION_PATH.parent, DATA_DIR, ARCHIVE_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "harvester.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("harvester")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.error("config.yaml не найден")
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state() -> dict:
    if STATE_PATH.exists():
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_PATH)


def slugify(text: str, max_len: int = 40) -> str:
    cyr = {
        'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'yo','ж':'zh','з':'z',
        'и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r',
        'с':'s','т':'t','у':'u','ф':'f','х':'h','ц':'ts','ч':'ch','ш':'sh','щ':'sch',
        'ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
    }
    text = text.lower()
    out = []
    for ch in text:
        if ch in cyr:
            out.append(cyr[ch])
        elif ch.isascii() and (ch.isalnum() or ch in "-_"):
            out.append(ch)
        elif ch.isspace() or ch in "/\\|":
            out.append("-")
    s = re.sub(r"-+", "-", "".join(out)).strip("-_")
    return (s or "topic")[:max_len]


# ---------- TG Desktop сериализация ----------

ENTITY_TYPES = {
    MessageEntityBold: "bold", MessageEntityItalic: "italic",
    MessageEntityUnderline: "underline", MessageEntityStrike: "strikethrough",
    MessageEntityCode: "code", MessageEntityPre: "pre",
    MessageEntityUrl: "link", MessageEntityTextUrl: "text_link",
    MessageEntityMention: "mention", MessageEntityHashtag: "hashtag",
}


def serialize_entities(text, entities):
    if not text:
        return []
    if not entities:
        return [{"type": "plain", "text": text}]
    ents = sorted(entities, key=lambda e: e.offset)
    out, cursor = [], 0
    for e in ents:
        if e.offset > cursor:
            out.append({"type": "plain", "text": text[cursor:e.offset]})
        chunk = text[e.offset:e.offset + e.length]
        etype = ENTITY_TYPES.get(type(e), "plain")
        item = {"type": etype, "text": chunk}
        if etype == "text_link" and hasattr(e, "url"):
            item["href"] = e.url
        out.append(item)
        cursor = e.offset + e.length
    if cursor < len(text):
        out.append({"type": "plain", "text": text[cursor:]})
    return out


def peer_id_str(peer):
    if isinstance(peer, PeerUser):
        return f"user{peer.user_id}"
    if isinstance(peer, PeerChannel):
        return f"channel{peer.channel_id}"
    if isinstance(peer, PeerChat):
        return f"chat{peer.chat_id}"
    return "unknown"


async def message_to_dict(msg, sender_cache):
    if msg is None or msg.action is not None:
        return None
    sender_id = peer_id_str(msg.from_id) if msg.from_id else peer_id_str(msg.peer_id)
    sender_name = sender_cache.get(sender_id)
    if sender_name is None:
        try:
            entity = await msg.get_sender()
            if entity is None:
                sender_name = "Unknown"
            elif isinstance(entity, User):
                first = entity.first_name or ""
                last = entity.last_name or ""
                sender_name = (first + " " + last).strip() or (entity.username or "Unknown")
            elif isinstance(entity, (Channel, Chat)):
                sender_name = entity.title or "Channel"
            else:
                sender_name = "Unknown"
        except Exception:
            sender_name = "Unknown"
        sender_cache[sender_id] = sender_name

    text = msg.message or ""
    obj = {
        "id": msg.id,
        "type": "message",
        "date": msg.date.replace(tzinfo=None).isoformat() if msg.date else None,
        "date_unixtime": str(int(msg.date.timestamp())) if msg.date else None,
        "from": sender_name,
        "from_id": sender_id,
        "text": text,
        "text_entities": serialize_entities(text, msg.entities or []),
    }
    if msg.reply_to:
        if getattr(msg.reply_to, "reply_to_msg_id", None):
            obj["reply_to_message_id"] = msg.reply_to.reply_to_msg_id
        if getattr(msg.reply_to, "forum_topic", False):
            obj["forum_topic"] = True
            top = getattr(msg.reply_to, "reply_to_top_id", None) or msg.reply_to.reply_to_msg_id
            obj["topic_id"] = top
    if msg.edit_date:
        obj["edited"] = msg.edit_date.replace(tzinfo=None).isoformat()
        obj["edited_unixtime"] = str(int(msg.edit_date.timestamp()))
    if msg.media is not None:
        obj["media_type"] = type(msg.media).__name__.replace("MessageMedia", "").lower()
        if hasattr(msg.media, "document") and msg.media.document:
            for attr in msg.media.document.attributes:
                if hasattr(attr, "file_name"):
                    obj["file_name"] = attr.file_name
                    break
    if msg.forward:
        obj["forwarded_from"] = msg.forward.from_name or "Unknown"
    return obj


def write_archive(out_file, meta, messages, append=True):
    if append and out_file.exists():
        with open(out_file, encoding="utf-8") as f:
            archive = json.load(f)
        archive["messages"].extend(messages)
    else:
        archive = dict(meta)
        archive["messages"] = list(messages)
    tmp = out_file.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(archive, f, ensure_ascii=False, indent=1)
    tmp.replace(out_file)
    return len(archive["messages"])


# ---------- Форум: список топиков ----------
# Получаем через iter_messages с фильтром MessageActionTopicCreate.
# Это работает в любой версии Telethon независимо от наличия
# GetForumTopicsRequest в API.

async def get_forum_topics(client, entity):
    topics = []
    seen_ids = set()
    try:
        async for msg in client.iter_messages(entity, limit=None):
            if msg.action is not None and isinstance(msg.action, MessageActionTopicCreate):
                if msg.id not in seen_ids:
                    seen_ids.add(msg.id)
                    topics.append({"id": msg.id, "title": msg.action.title})
    except FloodWaitError as e:
        log.warning(f"get_forum_topics FloodWait {e.seconds}s")
    except Exception as e:
        log.warning(f"get_forum_topics: {e}")
    # Топик с id=1 (General) обычно нет в action-сообщениях — добавляем вручную
    if 1 not in seen_ids:
        topics.append({"id": 1, "title": "General"})
    # Сортируем по id (старые первыми)
    topics.sort(key=lambda t: t["id"])
    return topics


# ---------- Plain harvest ----------

async def harvest_plain(client, chat_cfg, state, entity, max_pull):
    chat_key = chat_cfg["key"]
    out_file = DATA_DIR / f"{chat_key}.json"
    last_id = state.get(chat_key, {}).get("last_message_id", 0)

    title = getattr(entity, "title", None) or getattr(entity, "username", "Unknown")
    chat_type = (
        "public_supergroup" if isinstance(entity, Channel) and entity.megagroup else
        "channel" if isinstance(entity, Channel) else
        "private_group" if isinstance(entity, Chat) else
        "private_chat"
    )

    log.info(f"[{chat_key}] plain, last_id={last_id}")
    sender_cache = {}
    new_messages = []
    max_id_seen = last_id

    try:
        async for msg in client.iter_messages(entity, min_id=last_id,
                                              limit=max_pull, reverse=True):
            d = await message_to_dict(msg, sender_cache)
            if d is not None:
                new_messages.append(d)
            if msg.id > max_id_seen:
                max_id_seen = msg.id
    except FloodWaitError as e:
        log.warning(f"[{chat_key}] FloodWait {e.seconds}s")
        return 0
    except Exception as e:
        log.exception(f"[{chat_key}]: {e}")
        return 0

    if not new_messages:
        log.info(f"[{chat_key}] нет новых")
        return 0

    total = write_archive(out_file, {
        "name": title, "type": chat_type, "id": getattr(entity, "id", 0),
    }, new_messages, append=True)

    state.setdefault(chat_key, {})
    state[chat_key]["last_message_id"] = max_id_seen
    state[chat_key]["last_harvest"] = datetime.now(timezone.utc).isoformat()
    state[chat_key]["total_messages"] = total
    save_state(state)
    log.info(f"[{chat_key}] +{len(new_messages)} (всего: {total})")
    return len(new_messages)


# ---------- Forum harvest ----------

async def harvest_forum(client, chat_cfg, state, entity, max_pull):
    chat_key = chat_cfg["key"]
    title = getattr(entity, "title", chat_key)

    log.info(f"[{chat_key}] forum, тянем список топиков...")
    topics = await get_forum_topics(client, entity)
    log.info(f"[{chat_key}] топиков: {len(topics)}")

    state.setdefault(chat_key, {})
    state[chat_key].setdefault("topics", {})
    state[chat_key]["topic_list"] = [{"id": t["id"], "title": t["title"]} for t in topics]
    save_state(state)

    sender_cache = {}
    total_new = 0

    for t in topics:
        top_id = t["id"]
        ttitle = t["title"]
        slug = slugify(ttitle) or f"topic{top_id}"
        topic_key = f"topic_{top_id}"
        out_file = DATA_DIR / f"{chat_key}__{slug}-{top_id}.json"

        last_id = state[chat_key]["topics"].get(topic_key, {}).get("last_message_id", 0)
        log.info(f"  [{ttitle}] last_id={last_id}")

        new_messages = []
        max_id_seen = last_id

        try:
            async for msg in client.iter_messages(entity, min_id=last_id,
                                                  limit=max_pull, reverse=True,
                                                  reply_to=top_id):
                d = await message_to_dict(msg, sender_cache)
                if d is not None:
                    new_messages.append(d)
                if msg.id > max_id_seen:
                    max_id_seen = msg.id
        except FloodWaitError as e:
            log.warning(f"  [{ttitle}] FloodWait {e.seconds}s")
            continue
        except Exception as e:
            log.exception(f"  [{ttitle}]: {e}")
            continue

        if not new_messages:
            log.info(f"  [{ttitle}] нет новых")
            continue

        total = write_archive(out_file, {
            "name": f"{title} :: {ttitle}",
            "type": "forum_topic",
            "id": getattr(entity, "id", 0),
            "topic_id": top_id,
            "topic_title": ttitle,
        }, new_messages, append=True)

        state[chat_key]["topics"].setdefault(topic_key, {})
        state[chat_key]["topics"][topic_key]["last_message_id"] = max_id_seen
        state[chat_key]["topics"][topic_key]["title"] = ttitle
        state[chat_key]["topics"][topic_key]["last_harvest"] = datetime.now(timezone.utc).isoformat()
        state[chat_key]["topics"][topic_key]["total_messages"] = total
        save_state(state)
        log.info(f"  [{ttitle}] +{len(new_messages)} (всего: {total})")
        total_new += len(new_messages)
        await asyncio.sleep(1)

    state[chat_key]["last_harvest"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    log.info(f"[{chat_key}] forum total: +{total_new}")
    return total_new


async def harvest_chat(client, chat_cfg, state, max_pull):
    target = chat_cfg["target"]
    try:
        entity = await client.get_entity(target)
    except Exception as e:
        log.error(f"[{chat_cfg['key']}] не могу получить entity {target!r}: {e}")
        return 0

    is_forum_actual = isinstance(entity, Channel) and getattr(entity, "forum", False)
    forum_cfg = chat_cfg.get("forum")
    use_forum = is_forum_actual if forum_cfg is None else forum_cfg

    if use_forum and not is_forum_actual:
        log.warning(f"[{chat_cfg['key']}] forum=true в config, но чат не форум")
        use_forum = False

    if use_forum:
        return await harvest_forum(client, chat_cfg, state, entity, max_pull)
    return await harvest_plain(client, chat_cfg, state, entity, max_pull)


async def cmd_harvest(cfg, only=None, exclude=None):
    """Забрать новые сообщения.
    only:    список chat_key — забирать ТОЛЬКО эти чаты
    exclude: список chat_key — забирать всё КРОМЕ этих
    Если оба None — забирать всё что enabled.
    """
    max_pull = cfg.get("max_pull_per_chat", 5000)
    only_set = set(only) if only else None
    excl_set = set(exclude) if exclude else set()

    async with build_client(cfg) as client:
        state = load_state()
        for cc in cfg["chats"]:
            if not cc.get("enabled", True):
                continue
            key = cc["key"]
            if only_set is not None and key not in only_set:
                continue
            if key in excl_set:
                continue
            try:
                await harvest_chat(client, cc, state, max_pull)
            except Exception as e:
                log.exception(f"[{key}]: {e}")
            await asyncio.sleep(2)


# ---------- discover ----------

async def cmd_discover(cfg, search=None):
    async with build_client(cfg) as client:
        print(f"\n{'ID':>20}  {'Тип':<15}  Название")
        print("-" * 80)
        rows = []
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if isinstance(entity, User):
                kind = "Bot" if entity.bot else "User"
            elif isinstance(entity, Channel):
                if entity.megagroup:
                    kind = "Forum group" if getattr(entity, "forum", False) else "Supergroup"
                else:
                    kind = "Channel"
            elif isinstance(entity, Chat):
                kind = "Small group"
            else:
                kind = "Unknown"

            tid = entity.id
            if isinstance(entity, Channel):
                display_id = f"-100{tid}"
            elif isinstance(entity, Chat):
                display_id = f"-{tid}"
            else:
                display_id = str(tid)

            name = dialog.name or "(no name)"
            if search and search.lower() not in name.lower():
                continue
            rows.append((display_id, kind, name))

        for r in rows:
            print(f"{r[0]:>20}  {r[1]:<15}  {r[2]}")
        print(f"\nВсего: {len(rows)}")
        if search:
            print(f"(фильтр: {search!r})")


# ---------- archive ----------

async def cmd_archive(cfg, client=None):
    # Имя по дате — каждый день новый
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    arc_name = f"tg-archive-{today}.tar.gz"
    arc_path = ARCHIVE_DIR / arc_name

    files = list(DATA_DIR.glob("*.json"))
    if not files:
        log.warning("data/ пусто")
        return

    with tarfile.open(arc_path, "w:gz") as tar:
        for f in files:
            tar.add(f, arcname=f.name)
    log.info(f"архив: {arc_path}")

    async def _send_and_cleanup(c):
        me = await c.get_me()

        # Сначала удаляем старые архивы из Saved Messages (любые предыдущие
        # сообщения с файлом начинающимся на "tg-archive-")
        deleted_count = 0
        try:
            async for old_msg in c.iter_messages(me, limit=100):
                if old_msg.document is None:
                    continue
                old_name = None
                for attr in old_msg.document.attributes:
                    if hasattr(attr, "file_name"):
                        old_name = attr.file_name
                        break
                if old_name and old_name.startswith("tg-archive-") \
                        and old_name.endswith(".tar.gz") \
                        and old_name != arc_name:
                    try:
                        await old_msg.delete()
                        deleted_count += 1
                    except Exception as e:
                        log.warning(f"не удалить {old_name}: {e}")
            if deleted_count:
                log.info(f"удалено старых архивов в Saved: {deleted_count}")
        except Exception as e:
            log.warning(f"очистка Saved fail: {e}")

        # Отправляем свежий
        await c.send_file(me, arc_path,
                          caption=f"📦 TG archive {today} | "
                                  f"{len(files)} файлов | "
                                  f"{arc_path.stat().st_size//1024} KB"
                                  + (f" | удалено старых: {deleted_count}" if deleted_count else ""))

    if client is not None:
        await _send_and_cleanup(client)
    else:
        async with build_client(cfg) as c:
            await _send_and_cleanup(c)

    # Локальная ротация: удаляем локальные tar.gz старше 3 дней
    cutoff = datetime.now().timestamp() - 3 * 86400
    for old_arc in ARCHIVE_DIR.glob("tg-archive-*.tar.gz"):
        if old_arc.stat().st_mtime < cutoff and old_arc.name != arc_name:
            try:
                old_arc.unlink()
                log.info(f"локально удалён старый архив: {old_arc.name}")
            except Exception as e:
                log.warning(f"не удалить {old_arc.name}: {e}")

    # Ротация data/*.json если разрослись
    rotation_mb = cfg.get("rotation_size_mb", 50)
    for f in files:
        size_mb = f.stat().st_size / 1024 / 1024
        if size_mb > rotation_mb:
            week = ARCHIVE_DIR / today
            week.mkdir(exist_ok=True)
            shutil.copy2(f, week / f.name)
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
            d["messages"] = []
            with open(f, "w", encoding="utf-8") as fh:
                json.dump(d, fh, ensure_ascii=False, indent=1)
            log.info(f"rotate: {f.name} ({size_mb:.1f} MB)")


# ---------- dump ----------

async def cmd_dump(cfg, chat_key, hours, client=None):
    chat_cfg = next((c for c in cfg["chats"] if c["key"] == chat_key), None)
    if not chat_cfg:
        print(f"{chat_key} не найден в config.yaml")
        return
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    async def _run(c):
        entity = await c.get_entity(chat_cfg["target"])
        sender_cache = {}
        msgs = []
        async for msg in c.iter_messages(entity):
            if msg.date < cutoff:
                break
            d = await message_to_dict(msg, sender_cache)
            if d is not None:
                msgs.append(d)
        msgs.reverse()
        out = ARCHIVE_DIR / f"dump-{chat_key}-{int(datetime.now().timestamp())}.json"
        title = getattr(entity, "title", chat_key)
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"name": title, "messages": msgs}, f,
                      ensure_ascii=False, indent=1)
        log.info(f"dump: {out} ({len(msgs)})")
        me = await c.get_me()
        await c.send_file(me, out,
                          caption=f"🔍 {chat_key} за {hours}ч ({len(msgs)})")

    if client is not None:
        await _run(client)
    else:
        async with build_client(cfg) as c:
            await _run(c)


# ---------- listen ----------

async def cmd_listen(cfg):
    ctx = build_client(cfg)
    client = await ctx.__aenter__()
    me = await client.get_me()
    log.info(f"listener: {me.first_name}")

    @client.on(events.NewMessage(from_users=me.id))
    async def handler(event):
        text = (event.message.message or "").strip()
        if not text.startswith("/"):
            return
        parts = text.split()
        cmd = parts[0]
        try:
            if cmd == "/harvest":
                await event.reply("⏳ harvest...")
                state = load_state()
                total = 0
                for cc in cfg["chats"]:
                    if cc.get("enabled", True):
                        total += await harvest_chat(client, cc, state,
                                                    cfg.get("max_pull_per_chat", 5000))
                await event.reply(f"✅ +{total}")
            elif cmd == "/dump" and len(parts) >= 3:
                ck, hs = parts[1], int(re.sub(r"[^\d]", "", parts[2]) or "24")
                await event.reply(f"⏳ dump {ck} {hs}ч")
                await cmd_dump(cfg, ck, hs, client=client)
            elif cmd == "/archive":
                await event.reply("⏳ архив...")
                await cmd_archive(cfg, client=client)
            elif cmd == "/status":
                state = load_state()
                lines = ["📊 status:"]
                for cc in cfg["chats"]:
                    k = cc["key"]
                    s = state.get(k, {})
                    if "topics" in s:
                        total = sum(t.get("total_messages", 0) for t in s["topics"].values())
                        lines.append(f"  {k}: {len(s['topics'])} топиков, {total} msgs")
                    else:
                        lines.append(f"  {k}: {s.get('total_messages', 0)} msgs")
                await event.reply("\n".join(lines))
            elif cmd in ("/info", "/help"):
                info_text = (
                    "🤖 *TG Harvester — справка*\n"
                    "\n"
                    "Что это: собирает сообщения из 6 чатов и складывает в JSON-файлы,\n"
                    "чтобы можно было кидать их Claude как контекст.\n"
                    "\n"
                    "📋 *Команды:*\n"
                    "\n"
                    "▪️ `/status`\n"
                    "   Сводка по всем чатам: сколько сообщений собрано на текущий момент.\n"
                    "   Полезно посмотреть что система живая и накапливает данные.\n"
                    "\n"
                    "▪️ `/harvest`\n"
                    "   Запустить забор новых сообщений *прямо сейчас*, не дожидаясь\n"
                    "   расписания. Качает только то что появилось с прошлого захода.\n"
                    "   Занимает 10-60 секунд.\n"
                    "\n"
                    "▪️ `/archive`\n"
                    "   Упаковать *все* JSON-файлы в tar.gz и прислать сюда.\n"
                    "   Старые архивы в Saved Messages удаляются автоматически,\n"
                    "   остаётся только свежий.\n"
                    "\n"
                    "▪️ `/dump <chat_key> <hours>`\n"
                    "   Точечная выгрузка одного чата за последние N часов.\n"
                    "   Полезно когда хочешь посмотреть свежак конкретного чата\n"
                    "   без всех остальных.\n"
                    "   Примеры:\n"
                    "      /dump remnaflood 24    — ремнафлуд за сутки\n"
                    "      /dump prizrak-talk 6   — призрак за 6 часов\n"
                    "      /dump koala-clash 48   — коала за 2 суток\n"
                    "\n"
                    "📂 *Доступные чаты (chat_key):*\n"
                    "   `remnaflood` — форум (2 топика)\n"
                    "   `prizrak-talk`\n"
                    "   `koala-clash`\n"
                    "   `flclashx`\n"
                    "   `rabbit-hole-chat`\n"
                    "   `rabbit-hole-channel`\n"
                    "\n"
                    "🕐 *Автозапуски:*\n"
                    "   harvest — каждые 6 часов (03/09/15/20:30 UTC)\n"
                    "   archive — ежедневно в 20:50 UTC (23:50 МСК)\n"
                    "\n"
                    "💡 *Совет:* `/status` это самая безопасная команда —\n"
                    "вызови если не уверен жив ли harvester."
                )
                await event.reply(info_text)
        except Exception as e:
            log.exception("listener err")
            await event.reply(f"❌ {e}")

    await client.run_until_disconnected()


# ---------- TG client wrapper ----------

def _build_proxy_tuple(cfg):
    """Возвращает tuple для Telethon proxy= или None, если прокси не настроен."""
    p = cfg.get("proxy")
    if not p:
        return None
    import python_socks
    ptype_map = {
        "socks5": python_socks.ProxyType.SOCKS5,
        "socks4": python_socks.ProxyType.SOCKS4,
        "http": python_socks.ProxyType.HTTP,
    }
    ptype = ptype_map.get(p.get("type", "socks5").lower(), python_socks.ProxyType.SOCKS5)
    return (ptype, p["host"], int(p["port"]))


class build_client:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = None

    async def __aenter__(self):
        proxy = _build_proxy_tuple(self.cfg)
        self.client = TelegramClient(str(SESSION_PATH),
                                     self.cfg["api_id"], self.cfg["api_hash"],
                                     proxy=proxy)
        await self.client.start()
        return self.client

    async def __aexit__(self, *a):
        await self.client.disconnect()


async def cmd_auth(cfg):
    proxy = _build_proxy_tuple(cfg)
    if proxy:
        log.info(f"использую прокси {proxy[0]} {proxy[1]}:{proxy[2]}")
    client = TelegramClient(str(SESSION_PATH), cfg["api_id"], cfg["api_hash"],
                            proxy=proxy)
    await client.start()
    me = await client.get_me()
    log.info(f"авторизован: {me.first_name} (@{me.username}) id={me.id}")
    await client.disconnect()
    try:
        os.chmod(SESSION_PATH.with_suffix(".session"), 0o600)
    except Exception:
        pass


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cfg = load_config()
    action = sys.argv[1]

    if action == "auth":
        asyncio.run(cmd_auth(cfg))
    elif action == "discover":
        q = sys.argv[2] if len(sys.argv) > 2 else None
        asyncio.run(cmd_discover(cfg, q))
    elif action == "harvest":
        only = None
        exclude = None
        # Парсим --only key1,key2  или  --except key3,key4
        for i, arg in enumerate(sys.argv[2:], start=2):
            if arg == "--only" and i + 1 < len(sys.argv):
                only = [k.strip() for k in sys.argv[i + 1].split(",") if k.strip()]
            elif arg == "--except" and i + 1 < len(sys.argv):
                exclude = [k.strip() for k in sys.argv[i + 1].split(",") if k.strip()]
        asyncio.run(cmd_harvest(cfg, only=only, exclude=exclude))
    elif action == "archive":
        asyncio.run(cmd_archive(cfg))
    elif action == "dump":
        if len(sys.argv) < 4:
            print("usage: harvester.py dump <chat_key> <hours>")
            sys.exit(1)
        asyncio.run(cmd_dump(cfg, sys.argv[2], int(sys.argv[3])))
    elif action == "listen":
        asyncio.run(cmd_listen(cfg))
    else:
        print(f"unknown: {action}")
        sys.exit(1)


if __name__ == "__main__":
    main()
