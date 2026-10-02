PRAGMA foreign_keys = OFF;

CREATE TABLE schema_meta (version INTEGER NOT NULL);
INSERT INTO schema_meta(version) VALUES (0);

CREATE TABLE endpoints (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    url TEXT NOT NULL,
    secret TEXT NOT NULL,
    max_in_flight INTEGER NOT NULL,
    enabled INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE events (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE deliveries (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    endpoint_id TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    first_attempt_at TEXT,
    available_at TEXT NOT NULL,
    locked_by TEXT,
    locked_until TEXT,
    last_error TEXT,
    last_status INTEGER,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE attempt_log (
    id INTEGER PRIMARY KEY,
    delivery_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    worker_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    result TEXT NOT NULL,
    status INTEGER,
    error TEXT,
    response TEXT
);

CREATE TABLE dead_letter (
    delivery_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    replay_count INTEGER NOT NULL DEFAULT 0,
    last_replayed_at TEXT
);

INSERT INTO endpoints VALUES
('ep_01J8K4F3P8P8M7Q2KCPZ0V7M2A','org_7d83f1','https://callback-gw.example.net/v2/payments?route=cn-east','fixture_key_org_7d83f1',3,1,'2026-09-18T03:11:42.918204+00:00'),
('ep_01J8K4H77TGBHFF2B2ME9P1VMT','org_7d83f1','https://audit-gw.example.net/events/payment','fixture_key_audit_7d83f1',1,1,'2026-09-18T03:14:05Z'),
('ep_01J8M0CFVK2HE2D62JAA0NCQ1G','org_c91a20','https://partner-relay.example.net/hooks/billing','fixture_key_org_c91a20',2,1,'2026-09-19T21:04:13+00:00');

INSERT INTO events VALUES
('evt_01J8QAQ2AFY84R1C0PS2NW3YH4','org_7d83f1','payment.captured','{"currency":"CNY", "amount":43800, "payment_id":"pay_8fa31c", "merchant_ref":"SO-20260920-18473"}','capture:pay_8fa31c','2026-09-20T08:42:55.117300+00:00'),
('evt_01J8QAR8Q9HSDZKCB6Q5X8K3AF','org_7d83f1','refund.created','{"refund_id":"rf_1d224a","reason":"duplicate","amount":43800}','refund:rf_1d224a','2026-09-20T08:43:36Z'),
('evt_01J8QB5N7ZX2K9JY2E3DQQ6K32','org_c91a20','invoice.overdue','{"invoice_id":"inv_91c7", "days_overdue": 3, "contact_locale":"zh-CN"}','invoice:inv_91c7:overdue:3','2026-09-20T08:50:47+00:00');

INSERT INTO deliveries VALUES
('dlv_01J8QAQ2G1G4X66QGZ0K8M5T4A','evt_01J8QAQ2AFY84R1C0PS2NW3YH4','ep_01J8K4F3P8P8M7Q2KCPZ0V7M2A','queued',0,NULL,'2026-09-20T08:42:55.117300+00:00',NULL,NULL,NULL,NULL,'2026-09-20T08:42:55.117300+00:00',NULL),
('dlv_01J8QAQ2H6JYYR6X13X45QTYM0','evt_01J8QAQ2AFY84R1C0PS2NW3YH4','ep_01J8K4H77TGBHFF2B2ME9P1VMT','processing',1,'2026-09-20T08:43:12Z','2026-09-20T08:42:55Z','worker-03','2026-09-20T08:43:42Z',NULL,NULL,'2026-09-20T08:42:55Z',NULL),
('dlv_01J8QAR8V2GNEF9A9Q4WEX24CC','evt_01J8QAR8Q9HSDZKCB6Q5X8K3AF','ep_01J8K4F3P8P8M7Q2KCPZ0V7M2A','retrying',2,'2026-09-20T08:43:41Z','2026-09-20T08:44:19.553210+00:00',NULL,NULL,'http:503',503,'2026-09-20T08:43:36Z',NULL),
('dlv_01J8QB5NC0JYH3C0FRA8V1S5N9','evt_01J8QB5N7ZX2K9JY2E3DQQ6K32','ep_01J8M0CFVK2HE2D62JAA0NCQ1G','delivered',1,'2026-09-20T08:50:48Z','2026-09-20T08:50:47Z',NULL,NULL,NULL,204,'2026-09-20T08:50:47Z','2026-09-20T08:50:49Z'),
('dlv_01J8QAR91NQ8KZP6D5FHM7B0VT','evt_01J8QAR8Q9HSDZKCB6Q5X8K3AF','ep_01J8K4H77TGBHFF2B2ME9P1VMT','failed',3,'2026-09-20T08:43:40Z','2026-09-20T08:43:36Z',NULL,NULL,'attempts_exhausted',503,'2026-09-20T08:43:36Z','2026-09-20T08:46:13Z');

INSERT INTO attempt_log VALUES
(1,'dlv_01J8QAQ2H6JYYR6X13X45QTYM0',1,'worker-03','2026-09-20T08:43:12.420000+00:00',NULL,'started',NULL,NULL,NULL),
(2,'dlv_01J8QAR8V2GNEF9A9Q4WEX24CC',1,'worker-02','2026-09-20T08:43:41Z','2026-09-20T08:43:41.805000+00:00','retry',503,'http:503','upstream temporarily unavailable'),
(3,'dlv_01J8QAR8V2GNEF9A9Q4WEX24CC',2,'worker-06','2026-09-20T08:44:02Z','2026-09-20T08:44:02.332000+00:00','retry',429,'http:429','rate limited'),
(4,'dlv_01J8QB5NC0JYH3C0FRA8V1S5N9',1,'worker-01','2026-09-20T08:50:48Z','2026-09-20T08:50:49Z','success',204,NULL,''),
(5,'dlv_01J8QAR91NQ8KZP6D5FHM7B0VT',1,'worker-04','2026-09-20T08:43:40Z','2026-09-20T08:43:41Z','retry',503,'http:503','gateway unavailable'),
(6,'dlv_01J8QAR91NQ8KZP6D5FHM7B0VT',2,'worker-04','2026-09-20T08:44:08Z','2026-09-20T08:44:09Z','retry',503,'http:503','gateway unavailable'),
(7,'dlv_01J8QAR91NQ8KZP6D5FHM7B0VT',3,'worker-07','2026-09-20T08:46:12Z','2026-09-20T08:46:13Z','failure',503,'attempts_exhausted','gateway unavailable');

INSERT INTO dead_letter VALUES
('dlv_01J8QAR91NQ8KZP6D5FHM7B0VT','attempts_exhausted','2026-09-20T08:46:13.204000+00:00',1,'2026-09-20T08:45:01Z');

