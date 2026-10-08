# مرجع تنظیمات

[English](CONFIGURATION.md) · **فارسی**

<div dir="rtl">

سیمورغ همه‌چیز را در دو فایل TOML نگه می‌دارد. هر دو متن ساده‌اند، به‌صورت اتمیک
نوشته می‌شوند و مجوز `0600` دارند.

| سرور | فایل |
|---|---|
| خارج (اگزیت) | `/etc/simurgh/exit.toml` |
| ایران (رله) | `/etc/simurgh/relay.toml` |
| نصب کاربر عادی | `~/.simurgh/…` (با `--home` یا `SIMURGH_HOME` قابل تغییر) |
| رمز پنل | `/etc/simurgh/state.json` |
| گواهی‌ها | `/etc/simurgh/cert/{cert,key}.pem` |

تغییرات را با `simurgh restart` اعمال کنید. پورت‌ها را می‌توان بدون ری‌استارت هم
عوض کرد: از پنل یا با `simurgh mapping …`.

---

## `exit.toml` — سرور خارج

```toml
token = "kQ0…"                  # کلید مشترک، در دو سرور یکسان
name = "omega-de"               # نامی که در رله و پنل دیده می‌شود
cert_auto = true                # ساخت گواهی خودامضا در صورت نیاز
# cert_file = "/etc/letsencrypt/live/panel.example.com/fullchain.pem"
# key_file  = "/etc/letsencrypt/live/panel.example.com/privkey.pem"
allow_ips = []                  # لیست مجاز همتایان تونل (اختیاری)
push_ports = [443, 2053, 2083]  # پورت‌هایی که به رله پیشنهاد می‌شود
push_enabled = true
strict_ports = false            # true = فقط پورت‌های push_ports قابل اتصال‌اند
speedtest_port = 8808           # اندپوینت تست سرعت، فقط روی 127.0.0.1 (0 = خاموش)
proxy_protocol = "off"          # off | v1 | v2 — انتقال IP واقعی کاربر به پنل
log_level = "info"              # debug | info | warning | error

[[listen]]                      # برای هر اندپوینت تونل یک بلوک
carrier = "tls"                 # tls | wss | raw | plain
host = "0.0.0.0"
port = 443
path = "/ws"                    # مسیر HTTP برای wss (و tls)
fallback = "decoy"              # decoy | close — رفتار با غریبه‌ها
padding = true                  # استتار طول فریم‌ها
enabled = true
```

### حالت معکوس روی اگزیت

به‌جای گوش‌دادن، اگزیت به رله وصل می‌شود:

```toml
token = "kQ0…"
name = "omega-de"

[[listen]]
carrier = "tls"
port = 8443                     # پورتی که *رله* روی آن گوش می‌دهد
dial = "203.0.113.9"            # آدرس رله — همین خط حالت معکوس را فعال می‌کند
fingerprint = "AB:CD:…"         # پین گواهی رله (توصیه‌شده)
# insecure_skip_verify = true   # فقط برای تست
enabled = true
```

اگزیت خودش با عقب‌نشینی ۰.۵ تا ۱۵ ثانیه دوباره وصل می‌شود؛ ری‌استارت هیچ‌کدام از
دو طرف کار دستی لازم ندارد.

### کلیدهای مهم `exit.toml`

| کلید | پیش‌فرض | معنی |
|---|---|---|
| `token` | — | اجباری؛ کلید HMAC تونل |
| `name` | نام میزبان | نمایش در سمت رله |
| `cert_auto` | `true` | ساخت `/etc/simurgh/cert/*.pem` وقتی گواهی داده نشده |
| `allow_ips` | `[]` | اگر پر باشد، فقط این همتایان می‌توانند تونل باز کنند |
| `push_ports` | پورت‌های رایج پنل | پیشنهاد پورت‌ها به رله |
| `strict_ports` | `false` | رد کردن پورت‌هایی که به رله پیشنهاد نشده‌اند |
| `speedtest_port` | `8808` | اندپوینت سرعت، فقط روی `127.0.0.1` |
| `proxy_protocol` | `off` | ارسال هدر PROXY protocol به سرویس محلی |
| `log_level` | `info` | سطح لاگ |

### کلیدهای `[[listen]]`

| کلید | پیش‌فرض | معنی |
|---|---|---|
| `carrier` | `tls` | نوع استتار این اندپوینت |
| `host` / `port` | `0.0.0.0` / `8443` | آدرس گوش‌دادن (حالت مستقیم) |
| `path` | `/ws` | مسیر HTTP برای `wss` |
| `fallback` | `decoy` | `decoy` = سایت طعمه، `close` = قطع بی‌صدا |
| `decoy_file` | — | فایل HTML دلخواه برای طعمه |
| `padding` | `true` | پد کردن فریم‌ها |
| `enabled` | `true` | خاموش کردن بدون حذف بلوک |
| `dial` | — | **حالت معکوس**: وصل شدن به این آدرس به‌جای گوش‌دادن |
| `fingerprint` | — | اثرانگشت SHA-256 گواهی رله (معکوس) |
| `insecure_skip_verify` | `false` | پذیرش هر گواهی (فقط تست) |

---

## `relay.toml` — سرور ایران

```toml
token = "kQ0…"
name = "ir-tehran"
dial = "relay"                  # relay = مستقیم، exit = معکوس
panel_port = 8787
accept_push = true              # پذیرش لیست پورت‌هایی که اگزیت پیشنهاد می‌دهد
keepalive = 25                  # فاصلهٔ پینگ (ثانیه)
stream_window = 262144          # پنجرهٔ دریافتی هر استریم (بایت)
chunk = 65536                   # حداکثر بار هر فریم
log_level = "info"

[exit]                          # سرور خارجی که به آن وصل می‌شویم (حالت مستقیم)
carrier = "tls"
address = "203.0.113.9"
port = 443
domain = "panel.example.com"    # SNI (اختیاری)
path = "/ws"
fingerprint = "AB:CD:…"         # پین گواهی (اختیاری)
insecure_skip_verify = false

[[pool]]                        # سرورهای خارجی جایگزین برای فِیل‌اُور
address = "198.51.100.7"
port = 443
carrier = "tls"

[[mapping]]                     # فوروارد پورت: ایران -> خارج
name = "panel"
listen = 443                    # پورت روی همین سرور (ایران)
target_host = "127.0.0.1"       # اگزیت به این آدرس وصل می‌شود
target_port = 443
udp = false
enabled = true
```

### حالت معکوس روی رله

```toml
token = "kQ0…"
name = "ir-tehran"
dial = "exit"                   # منتظر اتصال سرور خارج می‌مانیم
panel_port = 8787

[tunnel]                        # شنونده‌ای که اگزیت به آن وصل می‌شود
carrier = "tls"
host = "0.0.0.0"
port = 8443                     # این پورت را در فایروال باز کنید
path = "/ws"
fallback = "decoy"
cert_auto = true                # گواهی برای همین شنونده
```

در حالت معکوس بلوک `[exit]` وجود ندارد؛ رله هیچ‌جا وصل نمی‌شود. بلوک‌های
`[[mapping]]` دقیقاً مثل قبل می‌مانند، چون پورت‌های کاربران همیشه روی سرور ایران
هستند.

### کلیدهای مهم `relay.toml`

| کلید | پیش‌فرض | معنی |
|---|---|---|
| `token` | — | اجباری؛ باید برابر توکن اگزیت باشد |
| `dial` | `relay` | `relay` = مستقیم، `exit` = معکوس |
| `panel_port` | `8787` | پورت پنل وب |
| `accept_push` | `true` | اعمال لیست پورتی که اگزیت می‌فرستد |
| `keepalive` | `25` | فاصلهٔ پینگ |
| `stream_window` | `262144` | پنجرهٔ دریافتی هر استریم (روی لینک‌های خیلی پرسرعت بالاتر ببرید) |
| `chunk` | `65536` | حداکثر بار فریم |
| `[exit]` | — | اندپوینت خارج (حالت مستقیم) |
| `[tunnel]` | — | شنوندهٔ حالت معکوس |
| `[[pool]]` | `[]` | سرورهای خارجی جایگزین |
| `[[mapping]]` | `[]` | فوروارد پورت‌ها |

### کلیدهای `[[mapping]]`

| کلید | پیش‌فرض | معنی |
|---|---|---|
| `listen` | — | پورت باز‌شده روی سرور ایران |
| `listen_host` | `0.0.0.0` | آدرس اتصال آن پورت |
| `target_host` | `127.0.0.1` | آدرسی که **از سمت اگزیت** استفاده می‌شود |
| `target_port` | — | پورت سرویس روی سرور خارج |
| `udp` | `false` | فوروارد UDP به‌جای TCP |
| `name` | — | برچسب در پنل و CLI |
| `enabled` | `true` | نگه‌داشتن رکورد ولی توقف فوروارد |

معادل‌های CLI:

```bash
simurgh mapping add 443 443 --name panel
simurgh mapping add 2096 2096 --udp
simurgh mapping add 8443 8443 --target-host 10.0.0.5
simurgh mapping toggle 443
simurgh mapping remove 443
```

---

## لینک‌های اتصال

یک لینک اتصال، توکن و اندپوینت را در یک رشته جا می‌دهد:

```
simurgh://<host>:<panel-port>/setup?u=<panel-user>&p=<base64 password>#<name>
```

* روی **اگزیت**: `simurgh link --show` لینک یک رلهٔ جدید را چاپ می‌کند.
* روی **رلهٔ معکوس**: همان فرمان لینک مخصوص اگزیت را چاپ می‌کند.
* روی سرور دیگر: `simurgh join '<link>'` کانفیگ را می‌سازد؛ `--host <ip>` برای
  تغییر آدرس و `--force` برای بازنویسی کانفیگ موجود.

لینک‌ها را محرمانه بدانید: توکن تونل داخل آن‌هاست.

## سرویس‌های systemd

| سرویس | نقش |
|---|---|
| `simurgh-exit.service` | پروسهٔ سرور خارج |
| `simurgh-relay.service` | پروسهٔ سرور ایران |

```bash
systemctl status simurgh-relay
journalctl -u simurgh-relay -f          # یا: simurgh logs -f
```

## متغیرهای محیطی

| متغیر | معنی |
|---|---|
| `SIMURGH_HOME` | پوشهٔ کانفیگ (پیش‌فرض `/etc/simurgh`) |
| `SIMURGH_ALLOW_NONROOT` | `1` — اجازهٔ نصب به‌عنوان کاربر عادی (بدون systemd) |
| `SIMURGH_REF` / `--ref` | رِفی که نصاب دانلود می‌کند (برنچ یا تگ) |
| `SIMURGH_REPO` | آدرس مخزن جایگزین |
| `SIMURGH_SRC`، `SIMURGH_VENV` | محل سورس و محیط مجازی |

</div>
