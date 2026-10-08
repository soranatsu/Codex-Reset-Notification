import json
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage

from .core import retry_after


class SendFailure(Exception):
    def __init__(self, code, retryable=True, uncertain=False, delay=60):
        self.code, self.retryable, self.uncertain, self.delay = code, retryable, uncertain, delay
        super().__init__(code)


class Transport:
    def __init__(self, secrets, smtp_factory=None, opener=urllib.request.urlopen):
        self.secrets, self.smtp_factory, self.opener = secrets, smtp_factory, opener

    def secret(self, key, config):
        try:
            return self.secrets.get(key, config)
        except OSError:
            raise SendFailure("系统凭据存储不可用；请在本机重新输入凭据", retryable=False) from None

    def smtp(self, config, destination, title, body):
        password = self.secret("smtp", config)
        if config["smtp_user"] and not password:
            raise SendFailure("SMTP 凭据尚未输入或无法读取", retryable=False)
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = config["sender"], destination, title
        message.set_content(body)
        client = None
        try:
            if self.smtp_factory:
                client = self.smtp_factory(config["smtp_host"], config["smtp_port"], timeout=20)
            elif config["smtp_tls"] == "SSL/TLS":
                client = smtplib.SMTP_SSL(config["smtp_host"], config["smtp_port"], timeout=20, context=ssl.create_default_context())
            else:
                client = smtplib.SMTP(config["smtp_host"], config["smtp_port"], timeout=20)
            client.ehlo()
            if config["smtp_tls"] == "STARTTLS":
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if config["smtp_user"]:
                client.login(config["smtp_user"], password)
            for command, value in ((client.mail, config["sender"]), (client.rcpt, destination)):
                code, _ = command(value)
                if code not in (250, 251):
                    raise SendFailure("SMTP 地址阶段拒绝 " + str(code), retryable=400 <= code < 500)
            try:
                code, _ = client.data(message.as_bytes())
            except (OSError, TimeoutError, smtplib.SMTPServerDisconnected):
                raise SendFailure("SMTP 发送后确认丢失；送达情况未知", uncertain=True) from None
            if code != 250:
                raise SendFailure("SMTP DATA 拒绝 " + str(code), retryable=400 <= code < 500)
        except SendFailure:
            raise
        except smtplib.SMTPResponseException as error:
            raise SendFailure("SMTP 拒绝 " + str(error.smtp_code), retryable=400 <= error.smtp_code < 500) from None
        except ssl.SSLError:
            raise SendFailure("TLS 证书验证或握手失败", retryable=False) from None
        except smtplib.SMTPNotSupportedError:
            raise SendFailure("SMTP 服务器不支持所选 TLS 或认证", retryable=False) from None
        except (OSError, TimeoutError, smtplib.SMTPException):
            raise SendFailure("SMTP 连接或认证阶段失败") from None
        finally:
            if client is not None:
                try:
                    client.close()
                except OSError:
                    pass
        return "SMTP 服务器已接受；未确认进入收件箱或被阅读"

    def bark(self, config, title, body, now):
        key = self.secret("bark", config)
        if not key:
            raise SendFailure("Bark 设备密钥尚未输入或无法读取", retryable=False)
        request = urllib.request.Request(config["bark_server"].rstrip("/") + "/push", data=json.dumps({
            "device_key": key, "title": title, "body": body, "group": "Codex Reset Notification", "level": "active"
        }, ensure_ascii=False).encode("utf-8"), headers={
            "Content-Type": "application/json", "User-Agent": "CodexResetNotification/1.0"}, method="POST")
        try:
            with self.opener(request, timeout=20) as response:
                payload = json.loads(response.read(65537))
            if not isinstance(payload, dict) or payload.get("code") != 200:
                raise SendFailure("Bark 返回未接受结果", retryable=False)
        except urllib.error.HTTPError as error:
            code, retryable = "Bark HTTP " + str(error.code), error.code == 429 or error.code >= 500
            delay = retry_after(error.headers.get("Retry-After"), now)
            error.close()
            raise SendFailure(code, retryable=retryable, delay=delay) from None
        except (OSError, TimeoutError, urllib.error.URLError, ValueError):
            raise SendFailure("Bark 请求确认丢失；送达情况未知", uncertain=True) from None
        return "Bark 服务器已接受；未确认设备显示或被阅读"

    def send(self, row, config, now):
        if row["channel"] == "smtp":
            return self.smtp(config, row["destination"], row["subject"], row["body"])
        return self.bark(config, row["subject"], row["body"], now)


class Sender:
    def __init__(self, store, transport):
        self.store, self.transport = store, transport

    def drain(self, config, now, stopped=lambda: False, only_ids=None):
        s = self.store
        with s.lock, s.db:
            s.db.execute("UPDATE queue SET status='cancelled',error='定时提醒已超出 30 分钟有效窗口' WHERE reminder=1 AND created<? AND status IN ('pending','retry','blocked')", (now - 1800,))
            channels = [key for key in ("smtp", "bark") if config[key + "_enabled"]]
            if not channels or only_ids == set():
                return
            query = "SELECT * FROM queue WHERE status IN ('pending','retry') AND due<=? AND channel IN (" + ",".join("?" for _ in channels) + ")"
            values = [now] + channels
            if only_ids is not None:
                query += " AND id IN (" + ",".join("?" for _ in only_ids) + ")"
                values.extend(sorted(only_ids))
            rows = s.db.execute(query + " ORDER BY id LIMIT 10", values).fetchall()
        for row in rows:
            if only_ids is not None and row["id"] not in only_ids:
                continue
            if stopped():
                break
            if not config[row["channel"] + "_enabled"]:
                continue
            if row["reminder"]:
                with s.lock:
                    batch = s.db.execute("SELECT tracking,stage FROM batches WHERE key=?", (row["batch"],)).fetchone()
                    fresh = (not s.meta("source_warning") and s.meta("last_fetch")
                             and now - float(s.meta("last_fetch")) <= max(600, config["interval"] + 60))
                if not config["reminders"] or not batch or not batch["tracking"] or not fresh:
                    continue
            with s.lock, s.db:
                updated = s.db.execute("UPDATE queue SET status='sending',tries=tries+1,updated=? WHERE id=? AND status IN ('pending','retry')", (now, row["id"])).rowcount
            if not updated:
                continue
            try:
                description = self.transport.send(row, config, now)
                status, error, due = "accepted", description, now
            except SendFailure as failure:
                attempt = row["tries"] + 1
                status = "uncertain" if failure.uncertain else ("retry" if failure.retryable and attempt < 8 else "blocked")
                error = failure.code
                due = now + max(failure.delay, min(3600, 60 * 2 ** min(attempt - 1, 8)))
            except Exception:
                # Never serialize third-party exceptions: their text may contain credentials.
                status, error, due = "uncertain", "发送发生未知错误；须人工核对后重试", now
            with s.lock, s.db:
                s.db.execute("UPDATE queue SET status=?,error=?,due=?,updated=? WHERE id=?", (status, error, due, now, row["id"]))
                s.log("SEND_" + status.upper(), "通知 #" + str(row["id"]) + "：" + error, now)


class DemoTransport:
    def send(self, row, config, now):
        return "离线模拟接受；没有连接 SMTP 或 Bark"
