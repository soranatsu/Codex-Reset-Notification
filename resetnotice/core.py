import copy
import hashlib
import json
import re
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE = "https://codex-reset.com/"
API = SOURCE + "api/"
UTC = timezone.utc
DEFAULTS = {
    "interval": 300, "timezone": "Etc/UTC", "reminders": True,
    "times": ["12:00", "18:00"], "reader_contact": "", "note": "",
    "smtp_enabled": False, "smtp_host": "", "smtp_port": 587,
    "smtp_tls": "STARTTLS", "smtp_user": "", "sender": "", "recipients": [],
    "smtp_saved": False, "bark_enabled": False,
    "bark_server": "https://api.day.app", "bark_saved": False,
}
STAGES = {
    "preview": "全局重置预告（尚未完成）", "reset": "全局重置已获公开确认",
    "announced": "可留存重置卡公告（尚未确认到账）", "arriving": "重置卡发放进展",
    "available": "官方报告重置卡可用（请核对本人账户）",
    "observed": "第三方监测账户到账（未确认您到账）",
}
FINAL = {"reset", "available"}
RANK = {"preview": 0, "reset": 3, "announced": 0, "arriving": 1,
        "available": 3, "observed": 2}


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def iso(seconds):
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds")


def contact_valid(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme == "mailto":
        return bool(re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", parsed.path)) and not parsed.query
    return (parsed.scheme == "https" and bool(parsed.hostname)
            and not parsed.username and not parsed.password and not parsed.fragment)


def validate(config, require_contact=False):
    if set(config) != set(DEFAULTS):
        raise ValueError("配置字段不兼容；配置中不能包含密码或设备密钥。")
    for key in ("reminders", "smtp_enabled", "smtp_saved", "bark_enabled", "bark_saved"):
        if type(config[key]) is not bool:
            raise ValueError("开关配置必须为布尔值。")
    if type(config["interval"]) is not int or not 60 <= config["interval"] <= 86400:
        raise ValueError("监测间隔须为 60 至 86400 秒。")
    try:
        ZoneInfo(config["timezone"])
    except (KeyError, ValueError, TypeError):
        raise ValueError("时区须为有效 IANA 名称，例如 Asia/Shanghai 或 Etc/UTC。") from None
    if not isinstance(config["times"], list) or len(config["times"]) > 12:
        raise ValueError("每天最多设置 12 个提醒时刻。")
    if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(v)) for v in config["times"]):
        raise ValueError("提醒时刻使用 HH:MM 格式。")
    for key in ("reader_contact", "note", "smtp_host", "smtp_user", "sender", "bark_server"):
        if not isinstance(config[key], str) or len(config[key]) > 500 or any(c in config[key] for c in "\r\n\x00"):
            raise ValueError("配置文本过长或包含换行。")
    if (require_contact or config["reader_contact"]) and not contact_valid(config["reader_contact"]):
        raise ValueError("请填写接口识别联系信息：自己的 https:// 主页或 mailto:联系邮箱。")
    if not config["reader_contact"].isascii():
        raise ValueError("接口联系信息请使用 ASCII URL 或邮箱，以便放入 User-Agent。")
    if type(config["smtp_port"]) is not int or not 1 <= config["smtp_port"] <= 65535:
        raise ValueError("SMTP 端口应为 1 至 65535。")
    if config["smtp_tls"] not in ("STARTTLS", "SSL/TLS", "无 TLS"):
        raise ValueError("请选择有效的 SMTP TLS 方式。")
    if not isinstance(config["recipients"], list) or len(config["recipients"]) > 30:
        raise ValueError("最多支持 30 个收件邮箱。")
    for address in [config["sender"]] + config["recipients"]:
        if address and (not isinstance(address, str) or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", address)):
            raise ValueError("邮箱须为纯地址，例如 person@example.invalid。")
    if len(set(config["recipients"])) != len(config["recipients"]) or "" in config["recipients"]:
        raise ValueError("收件邮箱不能重复或留空。")
    if config["smtp_enabled"]:
        if not config["smtp_host"] or not config["sender"] or not config["recipients"]:
            raise ValueError("启用邮件时须填写 SMTP 服务器、发件人和收件人。")
        if not re.fullmatch(r"[A-Za-z0-9.:-]+", config["smtp_host"]):
            raise ValueError("SMTP 服务器须为主机名或 IP，不能填写 URL。")
        if config["smtp_tls"] == "无 TLS" and config["smtp_user"]:
            raise ValueError("为保护账户凭据，SMTP 登录必须启用 TLS。")
    bark = urllib.parse.urlsplit(config["bark_server"])
    if (bark.scheme != "https" or not bark.hostname or bark.username or bark.password
            or bark.query or bark.fragment or bark.path not in ("", "/")):
        raise ValueError("Bark 服务器须为 HTTPS 主机地址，密钥填写在独立密码框。")
    return config


def load_config(directory):
    path = Path(directory) / "config.json"
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        return validate(config)
    except (OSError, ValueError, TypeError):
        raise ValueError("配置文件损坏或不兼容，已停止启动；请从备份恢复 config.json。") from None


def save_config(directory, config):
    validate(config)
    path = Path(directory) / "config.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def retry_after(value, now):
    if not value:
        return 60
    try:
        return max(60, int(value))
    except (ValueError, TypeError):
        try:
            return max(60, int(parsedate_to_datetime(value).timestamp() - now))
        except (ValueError, TypeError, OverflowError):
            return 60


class FetchFailure(Exception):
    def __init__(self, code, delay=60):
        self.code, self.delay = code, delay
        super().__init__(code)


class PublicClient:
    def __init__(self, contact, opener=urllib.request.urlopen):
        if not contact_valid(contact):
            raise ValueError("缺少有效接口联系信息。")
        self.opener = opener
        self.user_agent = "CodexResetNotification/1.0 (+" + contact + "; Windows desktop)"

    def get(self, route, now):
        if route not in ("feed", "timeline"):
            raise ValueError("Unsupported public route")
        request = urllib.request.Request(API + route, headers={
            "User-Agent": self.user_agent, "Accept": "application/json"})
        try:
            with self.opener(request, timeout=20) as response:
                content = response.read(2_000_001)
                if len(content) > 2_000_000:
                    raise FetchFailure("响应过大")
                payload = json.loads(content)
                headers = {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as error:
            code, delay = "HTTP " + str(error.code), retry_after(error.headers.get("Retry-After"), now)
            error.close()
            raise FetchFailure(code, delay) from None
        except (OSError, urllib.error.URLError, TimeoutError):
            raise FetchFailure("网络连接失败") from None
        except (ValueError, UnicodeError):
            raise FetchFailure("公开接口返回无效 JSON") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise FetchFailure("公开接口结构变化")
        return payload, headers


def freshness(feed, pairs, now):
    fetched = timestamp(feed.get("fetched_at"))
    if feed.get("stale") is not False or fetched is None or now - fetched > 600:
        return "来源已过期或缺少新鲜度信息；暂停新公告处理和定时提醒。"
    if fetched > now + 300:
        return "来源时间超前；请核对电脑时钟。"
    for _, headers in pairs:
        expires = timestamp(headers.get("x-published-expires-at"))
        checked = timestamp(headers.get("x-published-checked-at"))
        if (expires is not None and expires < now) or (checked is not None and now - checked > 600):
            return "来源缓存已过期；暂停新公告处理和定时提醒。"
    return ""


def public_url(value):
    if not isinstance(value, str):
        return ""
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return ""
    if parsed.hostname not in ("x.com", "twitter.com", "codex-reset.com"):
        return ""
    return value[:500]


def official(value):
    return bool(re.fullmatch(r"https://(?:x|twitter)\.com/thsottiaux/status/\d+", value or ""))


def clean_text(value):
    # Public feed is untrusted; control characters never reach email headers or UI.
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(value or ""))[:2500]


def post_id(record):
    link = public_url(record.get("url"))
    match = re.search(r"/status/(\d+)$", link)
    value = match.group(1) if match else str(record.get("id", ""))
    return value if re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", value) else ""


@dataclass
class Notice:
    raw_id: str
    root_id: str
    kind: str
    stage: str
    at: float
    summary: str
    url: str


def normalize(feed, timeline):
    """Fail closed. Never use classifier internals, probability or personal observations as completion."""
    tweets = {post_id(t): t for t in feed.get("tweets", []) if isinstance(t, dict) and post_id(t)}
    notices = []
    banked_word = re.compile(r"banked\s+reset|reset\s+bank|重置卡|可留存.{0,6}重置", re.I)
    future = re.compile(r"tomorrow|next\s+(?:\d+\s+)?(?:hour|minute)|within\s+(?:the\s+)?(?:hour|\d+)|later\s+(?:today|in the day)|landing|arriving|will\s+(?:be\s+)?(?:reset|arrive|land)|明天|稍后|即将", re.I)
    for event in timeline["events"]:
        if not isinstance(event, dict):
            continue
        identity = post_id(event)
        at = timestamp(event.get("announced_at"))
        if not identity or at is None:
            continue
        url = public_url(event.get("url"))
        text = clean_text(event.get("summary"))
        tweet = tweets.get(identity, {})
        root = str(tweet.get("in_reply_to_tweet_id") or tweet.get("conversation_id") or identity)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", root):
            root = identity
        banked = bool(banked_word.search(text))
        state = event.get("banked_state")
        observed = (not official(url) or event.get("source") == "observed"
                    or bool(re.search(r"monitoring\s+accounts|observed on|监测账户", text, re.I)))
        if banked and state == "available" and observed:
            notices.append(Notice(identity, root, "observation", "observed", at, text, url or SOURCE + "banked-reset"))
            continue
        if event.get("group") == "reset" and event.get("announcement_state") == "announced" and official(url):
            # A forward window is not completion, even if announcement_state says announced.
            stage = "preview" if event.get("preview") is True or future.search(text) else "reset"
            notices.append(Notice(identity, root, "global", stage, at, text, url))
        if banked and official(url) and (event.get("group") == "credits" or state in ("announced", "arriving", "available", "unknown")
                                        or re.search(r"additional banked|extra.{0,15}reset.{0,8}bank", text, re.I)):
            if event.get("is_reply") is True and root == identity and state not in ("arriving", "available"):
                continue
            stage = state if state in ("announced", "arriving", "available") else "announced"
            notices.append(Notice(identity, root, "banked", stage, at, text, url))
    # Only explicit banked delivery replies to a known announcement; ordinary replies stay silent.
    roots = {n.raw_id for n in notices if n.kind == "banked"}
    for notice in notices:
        if notice.kind == "observation" and notice.root_id != notice.raw_id and notice.root_id in roots:
            notice.kind = "banked"
    for identity, tweet in tweets.items():
        state = tweet.get("banked_state")
        root = str(tweet.get("in_reply_to_tweet_id") or tweet.get("conversation_id") or "")
        text, url, at = clean_text(tweet.get("text")), public_url(tweet.get("url")), timestamp(tweet.get("at"))
        concrete = re.search(r"\b(?:EOD|PST|UTC|arriving|deposited|available|landed|by\s+\d)\b|到账|可用|发放", text, re.I)
        if (identity != root and root in roots and state in ("arriving", "available")
                and concrete and official(url) and at is not None):
            notices.append(Notice(identity, root, "banked", state, at, text, url))
    unique = {(n.raw_id, n.kind, n.stage): n for n in notices}
    return sorted(unique.values(), key=lambda n: (n.at, RANK[n.stage], n.raw_id))


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.directory / "state.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS batches(
            key TEXT PRIMARY KEY, kind TEXT NOT NULL, stage TEXT NOT NULL, first_at REAL NOT NULL,
            updated_at REAL NOT NULL, summary TEXT NOT NULL, url TEXT NOT NULL,
            tracking INTEGER NOT NULL, user_stopped INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS receipts(raw_id TEXT, kind TEXT, stage TEXT,
            PRIMARY KEY(raw_id,kind,stage));
          CREATE TABLE IF NOT EXISTS queue(
            id INTEGER PRIMARY KEY, token TEXT UNIQUE NOT NULL, batch TEXT, channel TEXT NOT NULL,
            destination TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
            reminder INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
            tries INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
            error TEXT NOT NULL DEFAULT '');
          CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY, at REAL, code TEXT, message TEXT);
        """)
        # A process crash during send is ambiguous. Do not automatically resend it.
        with self.db:
            self.db.execute("UPDATE queue SET status='uncertain',error='发送期间程序退出；需人工核对后重试' WHERE status='sending'")

    def meta(self, key, default=""):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def put(self, key, value):
        self.db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def log(self, code, message, now):
        self.db.execute("INSERT INTO logs(at,code,message) VALUES(?,?,?)", (now, code, message))
        self.db.execute("DELETE FROM logs WHERE id <= (SELECT COALESCE(MAX(id),0)-1000 FROM logs)")

    def targets(self, config):
        result = [("smtp", address) for address in config["recipients"]] if config["smtp_enabled"] else []
        if config["bark_enabled"]:
            result.append(("bark", "configured-device"))
        return result

    def enqueue(self, topic, batch, title, body, config, now, reminder=False):
        for channel, destination in self.targets(config):
            token = hashlib.sha256((topic + "|" + channel + "|" + destination).encode()).hexdigest()
            self.db.execute("""INSERT OR IGNORE INTO queue(token,batch,channel,destination,subject,body,reminder,due,created,updated)
                             VALUES(?,?,?,?,?,?,?,?,?,?)""",
                            (token, batch, channel, destination, "【重要｜额度重置】" + title, body, int(reminder), now, now, now))

    def ingest(self, notices, config, now):
        with self.lock, self.db:
            initial = not self.meta("baseline_at")
            fence = now if initial else float(self.meta("baseline_at"))
            for n in notices:
                key = n.kind + ":" + n.root_id
                old = self.db.execute("SELECT * FROM batches WHERE key=?", (key,)).fetchone()
                is_new = self.db.execute("INSERT OR IGNORE INTO receipts VALUES(?,?,?)", (n.raw_id, n.kind, n.stage)).rowcount
                if not is_new:
                    continue
                if old and RANK[n.stage] < RANK[old["stage"]]:
                    continue
                changed = not old or old["stage"] != n.stage
                live = not initial and (n.at >= fence or bool(old and changed))
                track = bool(live or (old and old["tracking"])) and n.kind != "observation" and n.stage not in FINAL
                if old and old["user_stopped"]:
                    track = False
                self.db.execute("""INSERT INTO batches(key,kind,stage,first_at,updated_at,summary,url,tracking)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                    stage=excluded.stage,updated_at=excluded.updated_at,summary=excluded.summary,
                    url=excluded.url,tracking=excluded.tracking""",
                                (key, n.kind, n.stage, old["first_at"] if old else n.at, n.at, n.summary, n.url, int(track)))
                if n.stage in FINAL:
                    self.db.execute("UPDATE queue SET status='cancelled',error='该批已有更新的公开完成证据' WHERE batch=? AND status IN ('pending','retry','blocked')", (key,))
                if live and changed:
                    body = (STAGES[n.stage] + "\n\n" + n.summary + "\n\n原始证据：" + n.url
                            + "\n\n公开公告不代表您本人到账；请在自己的账户核对。"
                            + "\n可留存重置卡的发放不等于额度已重置。"
                            + "\n\nData: codex-reset.com\n" + SOURCE)
                    self.enqueue(key + ":" + n.stage, key, STAGES[n.stage], body, config, now)
            if initial:
                self.put("baseline_at", now)
                self.log("BASELINE", "首次运行已建立基线；历史公告及历史批次定时提醒不推送。", now)
            self.put("last_ingest", now)

    def reminders(self, config, now):
        if not config["reminders"]:
            return
        local = datetime.fromtimestamp(now, ZoneInfo(config["timezone"]))
        with self.lock, self.db:
            batches = self.db.execute("SELECT * FROM batches WHERE tracking=1 AND user_stopped=0").fetchall()
            for batch in batches:
                if batch["stage"] in FINAL or batch["kind"] == "observation":
                    continue
                # Resume within 30 minutes: one local-date/slot token also covers DST folds.
                for slot in sorted(set(config["times"])):
                    hour, minute = map(int, slot.split(":"))
                    elapsed = local.hour * 60 + local.minute - hour * 60 - minute
                    planned = local.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()
                    if 0 <= elapsed < 30 and batch["first_at"] <= planned:
                        topic = f"{batch['key']}:reminder:{config['timezone']}:{local.date()}:{slot}"
                        body = (f"该批仍待公开完成确认：{STAGES[batch['stage']]}\n" + batch["summary"]
                                + "\n请自行核对账户；本人到账可在界面停止该批提醒。"
                                + "\n来源证据：" + batch["url"] + "\n\nData: codex-reset.com\n" + SOURCE)
                        self.enqueue(topic, batch["key"], "待完成批次定时提醒 " + slot, body, config, now, True)

    def set_tracking(self, key, enabled, now):
        with self.lock, self.db:
            batch = self.db.execute("SELECT * FROM batches WHERE key=?", (key,)).fetchone()
            if not batch or batch["stage"] in FINAL or batch["kind"] == "observation":
                return False
            self.db.execute("UPDATE batches SET tracking=?,user_stopped=? WHERE key=?", (int(enabled), int(not enabled), key))
            if not enabled:
                self.db.execute("UPDATE queue SET status='cancelled',error='用户停止该批提醒' WHERE batch=? AND reminder=1 AND status IN ('pending','retry','blocked')", (key,))
            self.log("TRACKING", "用户修改一个批次的本地提醒开关；公开证据状态未改变。", now)
            return True

    def cancel_reminders(self, now):
        with self.lock, self.db:
            self.db.execute("UPDATE queue SET status='cancelled',updated=?,error='用户关闭定时提醒' WHERE reminder=1 AND status IN ('pending','retry','blocked')", (now,))

    def snapshot(self):
        with self.lock:
            return {
                "meta": dict(self.db.execute("SELECT key,value FROM meta")),
                "batches": [dict(r) for r in self.db.execute("SELECT * FROM batches ORDER BY updated_at DESC LIMIT 200")],
                "queue": [dict(r) for r in self.db.execute("SELECT * FROM queue ORDER BY id DESC LIMIT 200")],
                "logs": [dict(r) for r in self.db.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 100")],
            }

    def retry(self, queue_id, now):
        with self.lock, self.db:
            return self.db.execute("UPDATE queue SET status='pending',due=?,updated=?,error='',tries=0 WHERE id=? AND status IN ('blocked','uncertain','retry')", (now, now, queue_id)).rowcount

    def close(self):
        with self.lock:
            self.db.close()


class Monitor:
    def __init__(self, store, sender, client_factory=PublicClient):
        self.store, self.sender, self.client_factory = store, sender, client_factory

    def tick(self, config, now, stopped=lambda: False):
        s = self.store
        with s.lock:
            due = float(s.meta("next_poll", "0"))
        if now >= due and not stopped():
            with s.lock, s.db:
                # Persist gate before HTTP: restart cannot bypass the minimum interval.
                s.put("next_poll", now + config["interval"])
            try:
                client = self.client_factory(config["reader_contact"])
                feed_pair = client.get("feed", now)
                timeline_pair = client.get("timeline", now)
                if stopped():
                    return
                warning = freshness(feed_pair[0], [feed_pair, timeline_pair], now)
                with s.lock, s.db:
                    s.put("source_warning", warning)
                    s.put("last_fetch", now)
                    s.put("source_fetched_at", feed_pair[0].get("fetched_at", ""))
                    s.put("source_newest_post_at", feed_pair[0].get("newest_post_at", ""))
                    s.put("failures", 0)
                    s.log("STALE" if warning else "FETCH_OK", warning or "公开接口检查成功。", now)
                if not warning:
                    s.ingest(normalize(feed_pair[0], timeline_pair[0]), config, now)
            except FetchFailure as error:
                with s.lock, s.db:
                    failures = int(s.meta("failures", "0")) + 1
                    wait = max(error.delay, min(3600, config["interval"] * 2 ** min(failures - 1, 8)))
                    s.put("failures", failures)
                    s.put("next_poll", now + wait)
                    s.put("source_warning", error.code + "；等待后重试，恢复后补查可用历史。")
                    s.log("FETCH_FAIL", error.code + "；退避 " + str(wait) + " 秒。", now)
        with s.lock:
            fresh = (not s.meta("source_warning") and s.meta("last_fetch")
                     and now - float(s.meta("last_fetch")) <= max(600, config["interval"] + 60))
        if fresh and not stopped():
            s.reminders(config, now)
        if not stopped():
            self.sender.drain(config, now, stopped)
