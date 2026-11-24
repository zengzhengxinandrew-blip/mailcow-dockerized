import base64
import datetime as dt
import gc
import json
import os
import tempfile
import unittest

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import NameOID

from core import connect, init_db, record_ses_feedback
from ses_feedback import ses_feedback, verify_sns


TOPIC = "arn:aws:sns:ap-southeast-2:123456789012:wismail-feedback"
CERT_URL = "https://sns.ap-southeast-2.amazonaws.com/SimpleNotificationService-test.pem"
MESSAGE_ID = "<abc123@wiskind.top>"


def signed_envelope(event):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=1)).sign(key, hashes.SHA256(), default_backend()))
    envelope = {"Type": "Notification", "MessageId": "sns-1", "TopicArn": TOPIC,
                "Timestamp": "2026-11-23T00:00:00Z", "Message": json.dumps(event),
                "SigningCertURL": CERT_URL, "SignatureVersion": "2"}
    fields = ("Message", "MessageId", "Timestamp", "TopicArn", "Type")
    signed = "".join(field + "\n" + envelope[field] + "\n" for field in fields).encode()
    envelope["Signature"] = base64.b64encode(key.sign(signed, padding.PKCS1v15(), hashes.SHA256())).decode()
    return envelope, cert.public_bytes(Encoding.PEM)


class SesFeedbackTests(unittest.TestCase):
    def test_signed_delivery_and_tampering(self):
        event = {"eventType": "Delivery", "mail": {"headers": [{"name": "Message-ID", "value": MESSAGE_ID}]},
                 "delivery": {"recipients": ["buyer@example.com"]}}
        envelope, certificate = signed_envelope(event)
        fetch = lambda url: certificate
        verified = verify_sns(json.dumps(envelope).encode(), TOPIC, fetch)
        self.assertEqual(ses_feedback(verified)["message_id"], MESSAGE_ID)
        envelope["Message"] = "{}"
        with self.assertRaises(ValueError):
            verify_sns(json.dumps(envelope).encode(), TOPIC, fetch)
        envelope["SigningCertURL"] = "https://sns.ap-southeast-2.amazonaws.com.evil.test/a.pem"
        with self.assertRaises(ValueError):
            verify_sns(json.dumps(envelope).encode(), TOPIC, fetch)

    def test_feedback_matches_message_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "data.sqlite3")
            init_db(path)
            with connect(path) as db:
                db.execute("INSERT INTO batches(owner_email,name,state,created_at) VALUES (?,?,?,?)",
                           ("user01@wiskind.top", "test", "active", "2026-11-23T00:00:00Z"))
                db.execute("""INSERT INTO messages(batch_id,row_number,recipient_email,subject,body,status,message_id)
                              VALUES (1,1,'buyer@example.com','test','test','submitted',?)""", (MESSAGE_ID,))
            db.close()
            event = {"sns_message_id": "sns-1", "kind": "bounce", "message_id": MESSAGE_ID,
                     "recipients": ["buyer@example.com"], "permanent": True}
            self.assertEqual(record_ses_feedback(path, event), 1)
            self.assertEqual(record_ses_feedback(path, event), 0)
            with connect(path) as db:
                self.assertIsNotNone(db.execute("SELECT bounced_at FROM messages WHERE id=1").fetchone()[0])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM suppressions").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM delivery_events WHERE kind='bounce'").fetchone()[0], 1)
            db.close()
            gc.collect()

    def test_feedback_without_wismail_message_id_is_ignored(self):
        event = {"notificationType": "Delivery", "mail": {"headers": []},
                 "delivery": {"recipients": ["buyer@example.com"]}}
        envelope, certificate = signed_envelope(event)
        verified = verify_sns(json.dumps(envelope).encode(), TOPIC, lambda url: certificate)
        self.assertIsNone(ses_feedback(verified))
        event["mail"]["headers"] = [{"name": "Message-ID", "value": "<other-format>"}]
        envelope, certificate = signed_envelope(event)
        verified = verify_sns(json.dumps(envelope).encode(), TOPIC, lambda url: certificate)
        self.assertIsNone(ses_feedback(verified))

    def test_raw_notification_logs_structure_without_mail_content(self):
        raw = {"notificationType": "Delivery", "mail": {"source": "private@example.com"},
               "delivery": {"recipients": ["buyer@example.com"]}}
        with self.assertRaisesRegex(ValueError, "type=NoneType, keys=delivery,mail,notificationType") as caught:
            verify_sns(json.dumps(raw).encode(), TOPIC)
        self.assertNotIn("private@example.com", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
