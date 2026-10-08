import copy
import io
import json
import smtplib
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import Mock, patch

from resetnotice.core import (DEFAULTS, FetchFailure, Monitor, Notice, PublicClient,
                              Store, freshness, iso, load_config, normalize, retry_after,
                              save_config, validate)
from resetnotice.notify import DemoTransport, SendFailure, Sender, Transport
from resetnotice.vault import Secrets

NOW = datetime(2026, 10, 8, 11, 59, tzinfo=timezone.utc).timestamp()
SECRET = "fictional-test-secret-DO-NOT-LOG"


def configured():
    config = copy.deepcopy(DEFAULTS)
    config.update(smtp_enabled=True, smtp_host="smtp.example.invalid", sender="sender@example.invalid",
                  recipients=["one@example.invalid"], reader_contact="mailto:author@example.invalid")
    return config


def event(identity="1001", group="reset", text="The reset has been processed.", **extra):
    record = dict(id=identity, group=group, type=group, summary=text,
                  url="https://x.com/thsottiaux/status/" + identity, announced_at=iso(NOW + 1),
                  announcement_state="announced")
    record.update(extra)
    return record


def feed(events=None, **extra):
    payload = dict(events=events or [], tweets=[], fetched_at=iso(NOW), stale=False)
    payload.update(extra)
    return payload


def notice(identity="1001", stage="announced", root=None, at=NOW + 1, kind="banked"):
    return Notice(identity, root or identity, kind, stage, at, "Synthetic public announcement", "https://x.com/thsottiaux/status/" + identity)


class MemoryVault:
    def __init__(self):
        self.data = {}
    def read(self, key):
        return self.data.get(key, "")
    def write(self, key, value):
        self.data[key] = value
    def delete(self, key):
        self.data.pop(key, None)


class Response(io.BytesIO):
    def __init__(self, payload, headers=None):
        super().__init__(json.dumps(payload).encode())
        self.headers = headers or {}


class ParserTests(unittest.TestCase):
    def parse(self, events, tweets=None):
        return normalize(feed(tweets=tweets or []), {"events": events})

    def test_global_reset_requires_documented_confirmation(self):
        self.assertEqual(self.parse([event()])[0].stage, "reset")
        self.assertEqual(self.parse([event(announcement_state="none")]), [])
        self.assertEqual(self.parse([event(announcement_state=None)]), [])

    def test_future_announcement_is_not_completed(self):
        for text, extra in (("Global reset landing tomorrow.", {}), ("A full reset will land at noon.", {}), ("Confirmed reset.", {"preview": True})):
            self.assertEqual(self.parse([event(text=text, **extra)])[0].stage, "preview")

    def test_poll_joke_reply_and_probability_are_ignored(self):
        events = [event(text="Vote reset or improvements", announcement_state="none"), event("1002", text="Good evening", announcement_state="none")]
        f = feed(tweets=[{"id": "1003", "text": "93% reset probability", "kind": "signal"}], signal={"active": True, "kind": "candidate"})
        self.assertEqual(normalize(f, {"events": events}), [])

    def test_banked_grant_never_becomes_global_reset(self):
        result = self.parse([event(group="credits", text="Loading a banked reset into every paid account.", banked_state="unknown")])
        self.assertEqual([(n.kind, n.stage) for n in result], [("banked", "announced")])

    def test_explicit_progress_reply_joins_original_batch(self):
        initial = event(group="credits", text="One banked reset announced.", banked_state="announced")
        replies = [{"id": "1002", "url": "https://x.com/thsottiaux/status/1002", "at": iso(NOW+2),
                    "text": "Will be there by EOD PST.", "banked_state": "arriving", "in_reply_to_tweet_id": "1001"},
                   {"id": "1003", "url": "https://x.com/thsottiaux/status/1003", "at": iso(NOW+3),
                    "text": "Good evening", "banked_state": "arriving", "in_reply_to_tweet_id": "1001"}]
        result = self.parse([initial], replies)
        self.assertEqual([(n.root_id, n.stage) for n in result], [("1001", "announced"), ("1001", "arriving")])

    def test_third_party_available_is_not_completion(self):
        result = self.parse([event(group="credits", text="Banked reset landed on our monitoring accounts.", banked_state="available", source="observed", url=None)])
        self.assertEqual((result[0].kind, result[0].stage), ("observation", "observed"))

    def test_official_available_can_complete_banked_public_batch(self):
        result = self.parse([event(group="credits", text="Banked reset is available for everyone.", banked_state="available")])
        self.assertEqual((result[0].kind, result[0].stage), ("banked", "available"))

    def test_missing_fields_fail_closed(self):
        self.assertEqual(self.parse([{}, "wrong", event(announced_at="broken"), event(url="https://evil.example.invalid/status/1001")]), [])

    def test_explicitly_linked_observation_keeps_batch_pending(self):
        records = [event(group="credits", text="One banked reset announced.", banked_state="announced"),
                   event("1002", group="credits", text="Banked reset reached monitoring accounts.", banked_state="available", source="observed", url=None)]
        result = self.parse(records, [{"id": "1002", "in_reply_to_tweet_id": "1001"}])
        self.assertEqual((result[-1].kind, result[-1].root_id, result[-1].stage), ("banked", "1001", "observed"))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="resetnotice-test-")
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory)
        self.config = configured()
    def tearDown(self):
        self.store.close()
        self.temp.cleanup()
    def baseline(self):
        self.store.ingest([], self.config, NOW)

    def test_first_run_has_no_historical_notifications_or_reminders(self):
        self.store.ingest([notice(at=NOW-100)], self.config, NOW)
        self.store.reminders(self.config, NOW+60)
        self.assertEqual(self.store.snapshot()["queue"], [])
        self.assertEqual(self.store.snapshot()["batches"][0]["tracking"], 0)

    def test_restart_deduplicates_lifecycle_and_different_progress_posts(self):
        self.baseline()
        self.store.ingest([notice(), notice("1002", "arriving", "1001", NOW+2)], self.config, NOW+3)
        self.store.close()
        self.store = Store(self.directory)
        self.store.ingest([notice(), notice("1002", "arriving", "1001", NOW+2), notice("1003", "arriving", "1001", NOW+4)], self.config, NOW+5)
        self.assertEqual(len(self.store.snapshot()["queue"]), 2)
        self.assertEqual(len(self.store.snapshot()["batches"]), 1)

    def test_late_archive_entry_older_than_fence_is_silent(self):
        self.baseline()
        self.store.ingest([notice(at=NOW-86400)], self.config, NOW+60)
        self.assertEqual(self.store.snapshot()["queue"], [])

    def test_daily_reminder_once_and_completion_cancels_pending(self):
        self.baseline()
        self.store.ingest([notice()], self.config, NOW+2)
        self.store.reminders(self.config, NOW+60)
        self.store.reminders(self.config, NOW+80)
        self.assertEqual(sum(q["reminder"] for q in self.store.snapshot()["queue"]), 1)
        self.store.ingest([notice("1002", "available", "1001", NOW+90)], self.config, NOW+90)
        self.store.reminders(self.config, NOW+21660)
        reminders = [q for q in self.store.snapshot()["queue"] if q["reminder"]]
        self.assertEqual([q["status"] for q in reminders], ["cancelled"])
        self.assertEqual(self.store.snapshot()["batches"][0]["tracking"], 0)

    def test_observation_does_not_stop_reminders(self):
        self.baseline()
        self.store.ingest([notice(), notice("1002", "observed", "1001", NOW+2)], self.config, NOW+3)
        self.store.reminders(self.config, NOW+60)
        self.assertEqual(self.store.snapshot()["batches"][0]["tracking"], 1)
        self.assertTrue(any(q["reminder"] for q in self.store.snapshot()["queue"]))

    def test_user_stop_and_global_reminder_disable(self):
        self.baseline()
        self.store.ingest([notice()], self.config, NOW+2)
        self.store.reminders(self.config, NOW+60)
        self.store.set_tracking("banked:1001", False, NOW+61)
        self.assertEqual([q["status"] for q in self.store.snapshot()["queue"] if q["reminder"]], ["cancelled"])
        self.store.ingest([notice("1002", "arriving", "1001", NOW+70)], self.config, NOW+70)
        self.assertFalse(self.store.snapshot()["batches"][0]["tracking"])
        self.config["reminders"] = False
        self.store.reminders(self.config, NOW+21660)
        self.assertEqual(sum(q["reminder"] for q in self.store.snapshot()["queue"]), 1)

    def test_timezone_and_old_slots_do_not_flood_after_resume(self):
        self.baseline()
        self.config["timezone"] = "Asia/Shanghai"
        self.config["times"] = ["12:00", "18:00"]
        morning = datetime(2026,10,9,3,0,tzinfo=timezone.utc).timestamp()
        self.store.ingest([notice(at=morning)], self.config, morning)
        self.store.reminders(self.config, morning+3600)
        self.store.reminders(self.config, morning+9*3600)
        self.assertEqual(sum(q["reminder"] for q in self.store.snapshot()["queue"]), 1)

    def test_dst_fold_has_one_local_date_slot(self):
        self.baseline()
        self.config.update(timezone="America/New_York", times=["01:30"])
        first = datetime(2026,11,1,5,30,tzinfo=timezone.utc).timestamp()
        self.store.ingest([notice(at=first-600)], self.config, first-600)
        self.store.reminders(self.config, first)
        self.store.reminders(self.config, first+3600)
        self.assertEqual(sum(q["reminder"] for q in self.store.snapshot()["queue"]), 1)

    def test_crash_during_send_is_uncertain_on_restart(self):
        self.baseline()
        self.store.ingest([notice()], self.config, NOW+2)
        with self.store.db:
            self.store.db.execute("UPDATE queue SET status='sending'")
        self.store.close()
        self.store = Store(self.directory)
        self.assertEqual(self.store.snapshot()["queue"][0]["status"], "uncertain")

    def test_failed_delivery_persists_and_does_not_resend_accepted_recipient(self):
        self.config["recipients"].append("two@example.invalid")
        self.baseline()
        self.store.ingest([notice()], self.config, NOW+2)
        transport = Mock()
        transport.send.side_effect = ["SMTP accepted", SendFailure("network")]
        Sender(self.store, transport).drain(self.config, NOW+2)
        self.store.close()
        self.store = Store(self.directory)
        transport.send.side_effect = None
        transport.send.return_value = "SMTP accepted"
        Sender(self.store, transport).drain(self.config, NOW+30)
        self.assertEqual(transport.send.call_count, 2)
        Sender(self.store, transport).drain(self.config, NOW+62)
        self.assertEqual(transport.send.call_count, 3)
        self.assertEqual({q["status"] for q in self.store.snapshot()["queue"]}, {"accepted"})

    def test_secret_echo_in_exception_never_reaches_queue_or_logs(self):
        self.baseline()
        self.store.ingest([notice()], self.config, NOW+2)
        transport = Mock()
        transport.send.side_effect = RuntimeError(SECRET)
        Sender(self.store, transport).drain(self.config, NOW+2)
        text = json.dumps(self.store.snapshot())
        self.assertNotIn(SECRET, text)
        self.assertIn("uncertain", text)

    def test_uncertain_delivery_requires_manual_retry(self):
        self.baseline()
        self.store.ingest([notice()], self.config, NOW+2)
        transport = Mock()
        transport.send.side_effect = SendFailure("lost acknowledgement", uncertain=True)
        sender = Sender(self.store, transport)
        sender.drain(self.config, NOW+2)
        sender.drain(self.config, NOW+1000)
        self.assertEqual(transport.send.call_count, 1)
        self.assertEqual(self.store.retry(self.store.snapshot()["queue"][0]["id"], NOW+1001), 1)

    def test_reminder_expiry_and_stale_source_prevent_late_send(self):
        self.baseline()
        self.store.ingest([notice()], self.config, NOW+2)
        self.store.reminders(self.config, NOW+60)
        with self.store.db:
            self.store.put("last_fetch", NOW+60)
            self.store.put("source_warning", "stale")
        sender = Sender(self.store, DemoTransport())
        sender.drain(self.config, NOW+61)
        self.assertEqual([q["status"] for q in self.store.snapshot()["queue"] if q["reminder"]], ["pending"])
        sender.drain(self.config, NOW+1900)
        self.assertEqual([q["status"] for q in self.store.snapshot()["queue"] if q["reminder"]], ["cancelled"])

    def test_poll_failure_backoff_recovery_catches_up(self):
        calls = []
        class Client:
            def get(inner, route, now):
                calls.append((route, now))
                if len(calls) == 1:
                    raise FetchFailure("HTTP 429", 600)
                if route == "feed":
                    return feed(fetched_at=iso(now)), {}
                return {"events": [event()]}, {}
        monitor = Monitor(self.store, Sender(self.store, DemoTransport()), lambda contact: Client())
        self.baseline()
        monitor.tick(self.config, NOW+2)
        self.assertEqual(float(self.store.meta("next_poll")), NOW+602)
        monitor.tick(self.config, NOW+300)
        self.assertEqual(len(calls), 1)
        monitor.tick(self.config, NOW+602)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.store.snapshot()["queue"][0]["status"], "accepted")

    def test_partial_first_fetch_and_stale_fetch_never_establish_baseline(self):
        class Partial:
            def get(inner, route, now):
                if route == "timeline":
                    raise FetchFailure("network")
                return feed(), {}
        Monitor(self.store, Sender(self.store, DemoTransport()), lambda contact: Partial()).tick(self.config, NOW)
        self.assertFalse(self.store.meta("baseline_at"))
        class Stale:
            def get(inner, route, now):
                return (feed(stale=True) if route == "feed" else {"events": [event()]}), {}
        Monitor(self.store, Sender(self.store, DemoTransport()), lambda contact: Stale()).tick(self.config, NOW+300)
        self.assertFalse(self.store.meta("baseline_at"))

    def test_poll_gate_survives_restart(self):
        client = Mock()
        client.get.side_effect = [(feed(), {}), ({"events": []}, {})]
        monitor = Monitor(self.store, Sender(self.store, DemoTransport()), lambda contact: client)
        self.config["interval"] = 60
        monitor.tick(self.config, NOW)
        self.store.close()
        self.store = Store(self.directory)
        Monitor(self.store, Sender(self.store, DemoTransport()), lambda contact: client).tick(self.config, NOW+59)
        self.assertEqual(client.get.call_count, 2)


class BoundaryTests(unittest.TestCase):
    def test_identifying_user_agent_and_get_only(self):
        seen = []
        def opener(request, timeout):
            seen.append(request)
            return Response({"events": []})
        client = PublicClient("mailto:author@example.invalid", opener)
        client.get("feed", NOW)
        self.assertEqual(seen[0].get_method(), "GET")
        self.assertIn("CodexResetNotification/1.0", seen[0].get_header("User-agent"))
        self.assertIn("mailto:author@example.invalid", seen[0].get_header("User-agent"))
        with self.assertRaises(ValueError):
            client.get("pray", NOW)

    def test_http429_retry_after_and_http_date(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 429, SECRET, {"Retry-After": "900"}, None)
        with self.assertRaises(FetchFailure) as caught:
            PublicClient("mailto:author@example.invalid", opener).get("feed", NOW)
        self.assertEqual(caught.exception.delay, 900)
        self.assertNotIn(SECRET, str(caught.exception))
        date = format_datetime(datetime.fromtimestamp(NOW+1200, timezone.utc), usegmt=True)
        self.assertEqual(retry_after(date, NOW), 1200)

    def test_network_and_schema_errors_are_safe(self):
        def opener(request, timeout):
            raise urllib.error.URLError(SECRET)
        with self.assertRaises(FetchFailure) as caught:
            PublicClient("mailto:author@example.invalid", opener).get("feed", NOW)
        self.assertNotIn(SECRET, str(caught.exception))
        with self.assertRaises(FetchFailure):
            PublicClient("mailto:author@example.invalid", lambda *args, **kw: Response({"wrong": []})).get("timeline", NOW)

    def test_freshness_uses_fetch_age_not_old_post_age(self):
        self.assertEqual(freshness(feed(newest_post_at=iso(NOW-86400*50)), [], NOW), "")
        self.assertTrue(freshness(feed(fetched_at=iso(NOW-601)), [], NOW))
        self.assertTrue(freshness(feed(), [({}, {"x-published-expires-at": iso(NOW-1)})], NOW))

    def test_config_validation_tls_injection_and_plaintext_secrets(self):
        for key, value in (("interval", 59), ("smtp_port", 0), ("sender", "safe@example.invalid\r\nBcc: bad"), ("bark_server", "https://api.day.app/SECRET"), ("timezone", "Invalid/Zone")):
            config = configured()
            config[key] = value
            with self.assertRaises(ValueError):
                validate(config)
        config = configured()
        config.update(smtp_tls="无 TLS", smtp_user="login@example.invalid")
        with self.assertRaises(ValueError):
            validate(config)
        config = configured()
        config["password"] = SECRET
        with self.assertRaises(ValueError):
            validate(config)

    def test_config_roundtrip_without_secret_and_corruption_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            config = configured()
            save_config(directory, config)
            self.assertEqual(load_config(directory), config)
            self.assertNotIn(SECRET, Path(directory, "config.json").read_text(encoding="utf-8"))
            Path(directory, "config.json").write_text("broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(directory)

    def test_explicit_credential_persistence_and_forget(self):
        vault = MemoryVault()
        secrets = Secrets(vault)
        config = configured()
        secrets.set("smtp", SECRET, False)
        self.assertEqual(vault.data, {})
        self.assertEqual(secrets.get("smtp", config), SECRET)
        secrets.set("smtp", SECRET, True)
        config["smtp_saved"] = True
        self.assertEqual(Secrets(vault).get("smtp", config), SECRET)
        secrets.set("smtp", SECRET, False)
        self.assertEqual(Secrets(vault).get("smtp", config), "")


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.config = configured()
        self.config["smtp_user"] = "login@example.invalid"
        vault = MemoryVault()
        self.secrets = Secrets(vault)
        self.secrets.set("smtp", SECRET, False)
        self.secrets.set("bark", SECRET, False)

    def smtp_client(self):
        client = Mock()
        client.mail.return_value = (250, b"OK")
        client.rcpt.return_value = (250, b"OK")
        client.data.return_value = (250, b"queued")
        return client

    def test_smtp_tls_login_single_recipient_and_acceptance_language(self):
        client = self.smtp_client()
        transport = Transport(self.secrets, smtp_factory=lambda *args, **kw: client)
        result = transport.smtp(self.config, "one@example.invalid", "test", "Data: codex-reset.com\nhttps://codex-reset.com/")
        client.starttls.assert_called_once()
        client.login.assert_called_once_with("login@example.invalid", SECRET)
        payload = client.data.call_args.args[0]
        self.assertIn(b"To: one@example.invalid", payload)
        self.assertNotIn(SECRET.encode(), payload)
        self.assertIn("未确认", result)

    def test_smtp_data_timeout_is_uncertain(self):
        client = self.smtp_client()
        client.data.side_effect = TimeoutError(SECRET)
        with self.assertRaises(SendFailure) as caught:
            Transport(self.secrets, smtp_factory=lambda *args, **kw: client).smtp(self.config, "one@example.invalid", "test", "body")
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn(SECRET, str(caught.exception))

    def test_smtp_auth_and_recipient_failure_never_log_server_echo(self):
        client = self.smtp_client()
        client.login.side_effect = smtplib.SMTPAuthenticationError(535, SECRET.encode())
        with self.assertRaises(SendFailure) as caught:
            Transport(self.secrets, smtp_factory=lambda *args, **kw: client).smtp(self.config, "one@example.invalid", "test", "body")
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn(SECRET, str(caught.exception))
        client.login.side_effect = None
        client.rcpt.return_value = (450, SECRET.encode())
        with self.assertRaises(SendFailure) as caught:
            Transport(self.secrets, smtp_factory=lambda *args, **kw: client).smtp(self.config, "one@example.invalid", "test", "body")
        self.assertTrue(caught.exception.retryable)
        client.data.assert_not_called()

    def test_bark_key_stays_in_post_body_and_no_critical_claim(self):
        seen = []
        def opener(request, timeout):
            seen.append(request)
            return Response({"code": 200})
        result = Transport(self.secrets, opener=opener).bark(self.config, "test", "body", NOW)
        self.assertNotIn(SECRET, seen[0].full_url)
        self.assertEqual(seen[0].get_method(), "POST")
        self.assertEqual(json.loads(seen[0].data)["level"], "active")
        self.assertIn("未确认", result)

    def test_bark_http429_honors_retry_after(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 429, SECRET, {"Retry-After": "7200"}, None)
        with self.assertRaises(SendFailure) as caught:
            Transport(self.secrets, opener=opener).bark(self.config, "test", "body", NOW)
        self.assertEqual(caught.exception.delay, 7200)
        self.assertNotIn(SECRET, str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
