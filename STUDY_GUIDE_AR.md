# دليل المذاكرة والمقابلات الهندسية لمشروع مُرَاقِب (`muraqib-telemetry`)

هذا الدليل يشرح كل مفهوم هندسي حقيقي في أنظمة المراقبة والاعتمادية (**Observability & SRE**) مع ربطه مباشرةً بالملفات والأسطر الفعلية داخل مستودع `muraqib-telemetry`.

---

## 1. نموذج السحب مقابل الدفع (Pull vs Push Model)
- **المفهوم:** في أنظمة المراقبة القديمة (مثل بعض وكلاء Graphite أو StatsD) يقوم الخادم المراقَب بدفع المقاييس (**Push**) إلى خادم مركزي؛ أما في **Prometheus** فالنظام يعتمد نموذج السحب (**Pull**): الخادم يفتح نقطة `/metrics` ويقوم Prometheus بسحبها كل فترة (`scrape_interval`).
- **ميزة الـ Pull:** إذا توقف التطبيق عن الاستجابة يعرف Prometheus فوراً أن الهدف `up == 0`، كما يسهل تشغيل عدة نسخ من Prometheus للمراقبة دون تعديل إعدادات التطبيق.
- **أين يوجد في المشروع؟**
  - نقطة التصدير: [`app/main.py`](app/main.py) في الدالة `metrics()` ومسار `GET /metrics`.
  - إعداد السحب في Prometheus: [`deploy/prometheus.yml`](deploy/prometheus.yml) (`scrape_interval: 5s`, `metrics_path: /metrics`, `targets: ["muraqib:8000"]`).

---

## 2. تنسيق Prometheus القياسي (`/metrics` Exposition Format v0.0.4)
- **المفهوم:** Prometheus يقرأ نصاً عادياً (`text/plain; version=0.0.4`) يحتوي على سطرين وصفيين لكل مقياس:
  - `# HELP <metric_name> <description>`
  - `# TYPE <metric_name> <gauge|counter|histogram|summary>`
  - ثم القيم مع الوسوم (`Labels`) مثل: `muraqib_target_up{target="api-gateway-health",protocol="http"} 1`
- **لماذا `Gauge` هنا؟** لأن نسبة المعالج `CPU %` أو الذاكرة `RAM %` أو زمن الاستجابة `Latency ms` ترتفع وتنخفض لحظياً، على عكس `Counter` الذي يزيد فقط.
- **أين يوجد في المشروع؟**
  - الدالة `render_prometheus_metrics()` في [`app/main.py`](app/main.py).

---

## 3. إرهاق التنبيهات والتباطؤ الوقائي (Alert Fatigue & Hysteresis)
- **المشكلة (Alert Fatigue):** إذا أطلقت تنبيهاً فورياً عند أول قراءة يتجاوز فيها المعالج 90% لثانية واحدة، سيتلقى فريق الـ NOC مئات التنبيهات الكاذبة (**False Positives**) مما يؤدي لتجاهل التنبيهات الحقيقية.
- **الحل (Hysteresis State Machine):** اعتماد نافذة استمرار (`consecutive_breaches`) قبل تفعيل التنبيه:
  - `OK` ← قراءة واحدة تتجاوز العتبة ← **`PENDING`** (انتظار التأكد، لا يُفتح حادث).
  - إذا عادت القراءة التالية للوضع الطبيعي ← يعود فوراً إلى **`OK`** (تم منع التنبيه الكاذب!).
  - إذا استمر التجاوز لـ `N` دورات متتالية (مثلاً 3 مرات) ← ينتقل إلى **`FIRING`** ويُفتح حادث في SQLite.
  - عند التعافي (`recovery_breaches = 2`) ← ينتقل إلى **`RESOLVED`** ثم **`OK`** بعد فترة التهدئة (`cooldown_seconds`).
- **أين يوجد في المشروع؟**
  - المحرك الكامل: `HysteresisAlertEngine` في [`app/alerts.py`](app/alerts.py).
  - قواعد Prometheus الموازية عبر شرط `for: 15s`: [`deploy/alert_rules.yml`](deploy/alert_rules.yml).
  - اختبار التحقق الآلي: `test_hysteresis_prevents_alert_fatigue_and_fires_on_sustained_breach` في [`tests/test_muraqib.py`](tests/test_muraqib.py).

---

## 4. الفرق بين MTTA و MTTR في إدارة الحوادث
- **MTTA (Mean Time To Acknowledge):** متوسط الزمن بين لحظة فتح الحادث (`opened_at`) ولحظة استلام مهندس المستوى الأول للحالة (`acknowledged_at`). يقيس سرعة استجابة فريق المراقبة.
- **MTTR (Mean Time To Resolve):** متوسط الزمن بين لحظة فتح الحادث (`opened_at`) ولحظة حلّه وإغلاقه بالكامل (`resolved_at`). يقيس كفاءة التشخيص والمعالجة.
- **أين يوجد في المشروع؟**
  - حساب القيمتين: `_compute_mtta_mttr()` في [`app/main.py`](app/main.py).
  - تخزين الطوابع الزمنية وتحديثها: `triage_incident()` في [`app/storage.py`](app/storage.py).

---

## 5. التصعيد المنظم من L1 إلى L2 (L1 → L2 Escalation & Diagnostic Runbooks)
- **المفهوم:** مهندس المستوى الأول (**L1 NOC**) لا يكتفي بتمرير رسالة "المعالج مرتفع" إلى مهندس المستوى الثاني (**L2 SRE/DBA**)، بل ينفذ فحوصات أولية موثقة من دليل التشغيل (**Runbook**) مثل `top` و `vmstat` و `iostat` و `ss` ثم يرفق الملاحظات مع التصعيد (`ESCALATED_L2`).
- **أين يوجد في المشروع؟**
  - كتالوج الأدلة التشخيصية (أوامر Linux و Windows PowerShell): `RUNBOOK_CATALOG` في [`app/alerts.py`](app/alerts.py).
  - نقطة التصعيد: `POST /api/incidents/{incident_id}/triage` في [`app/main.py`](app/main.py).
  - الدرج التفاعلي في الواجهة: `openRunbookDrawer()` في [`docs/index.html`](docs/index.html).

---

## 6. الفرق بين Load Average و CPU % و I/O Wait %
- **CPU Utilization %:** النسبة المئوية للوقت الذي تنفذ فيه الأنوية تعليمات فعلية.
- **I/O Wait %:** النسبة المئوية للوقت الذي يكون فيه المعالج خاملاً **ومعطّلاً في انتظار القرص أو الشبكة** لإتمام عملية قراءة/كتابة. ارتفاعها يعني غالباً عنق زجاجة في التخزين (`iostat -xz`) وليس ضعفاً في المعالج.
- **Load Average (1m):** متوسط عدد العمليات التي تنتظر دورها في طابور المعالج **أو** تنتظر إتمام I/O (في لينكس). لذلك قد ترى `Load Average` مرتفعاً بينما `CPU %` منخفض إذا كانت العمليات عالقة في `I/O Wait`!
- **أين يوجد في المشروع؟**
  - جمع المقاييس الثلاثة عبر `psutil` مع بديل المكتبة القياسية `stdlib` عند غيابها: `TelemetryCollector` في [`app/collector.py`](app/collector.py).

---

## 7. وضع SQLite WAL ودوره في التزامن
- **المفهوم:** تفعيل `PRAGMA journal_mode=WAL;` (Write-Ahead Logging) يسمح لقارئات لوحة المراقبة (`GET /api/snapshot`, `GET /api/incidents`) بالعمل بالتوازي دون حظر عمليات الكتابة (`insert_sample`, `create_incident`).
- **أين يوجد في المشروع؟**
  - تهيئة الاتصال والاستعلامات المحمية بالمعاملات (`?`): `MuraqibStorage._connect()` في [`app/storage.py`](app/storage.py).

---

## 8. تجميع البنية عبر Docker Compose
- **المفهوم:** ملف [`docker-compose.yml`](docker-compose.yml) يربط 3 حاويات في شبكة معزولة:
  1. `muraqib`: تطبيق FastAPI + الواجهة (`8000`).
  2. `prometheus`: يسحب `/metrics` من `muraqib:8000` ويقيّم [`deploy/alert_rules.yml`](deploy/alert_rules.yml) (`9090`).
  3. `grafana`: يعرض لوحة [`deploy/grafana-dashboard.json`](deploy/grafana-dashboard.json) (`3000`).
- **أين يوجد في المشروع؟**
  - [`Dockerfile`](Dockerfile) و [`docker-compose.yml`](docker-compose.yml) ومجلد [`deploy/`](deploy/).

---

## 9. أسئلة مقابلة سريعة (اختبر نفسك)
1. *لماذا لم يُطلق `muraqib` تنبيهاً عند حقن `cpu_spike` لدورة واحدة (`ticks=1`)؟* — بسبب آلية **Hysteresis** التي تتطلب 3 تجاوزات متتالية (`PENDING` ← `FIRING`) لمنع الـ Alert Fatigue.
2. *كيف يعمل `docs/index.html` على GitHub Pages بدون خادم Python؟* — يتحقق تلقائياً من `/api/snapshot`؛ وإذا لم يجد خادماً نشطاً يتحول بسلاسة إلى **Interactive Browser Lab Mode** بمحرك قياس وهسترة كامل داخل المتصفح.
3. *ماذا يحدث لو لم تكن مكتبة `psutil` مثبتة على الخادم؟* — ينتقل `TelemetryCollector` تلقائياً إلى `_collect_stdlib_fallback()` باستخدام `shutil.disk_usage` و `/proc` أو Windows `GlobalMemoryStatusEx`.
