<div align="center">
  <img src="assets/logo.png" width="184" alt="نشان سیمرغ تانل">

  # سیمرغ تانل

  **مسیرِ شفاف و پایدار میان سرور ایران و زیرساخت خارج.**

  فوروارد TCP و UDP · تونل مالتی‌پلکس · Python + Go · پنل فارسی/انگلیسی

  [![CI](https://github.com/Dark-Sky07/Simurgh-Tunnel/actions/workflows/ci.yml/badge.svg)](https://github.com/Dark-Sky07/Simurgh-Tunnel/actions/workflows/ci.yml) [![Release](https://img.shields.io/github/v/release/Dark-Sky07/Simurgh-Tunnel?display_name=tag)](https://github.com/Dark-Sky07/Simurgh-Tunnel/releases/latest) [![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

  [English](README.md) · **فارسی**<br>
  [شروع سریع](#quick-start) · [راهنمای مسیرهای ایران](#iran-playbook) · [پنل](#web-panel) · [امنیت](#security) · [مستندات](#documentation)
</div>

---

<div dir="rtl">

سیمرغ ترافیک پورت‌های عمومیِ **سرور ایران (رله)** را به سرویس‌های **سرور خارج (اگزیت)** می‌رساند، بدون اینکه برنامهٔ کاربر مجبور باشد تونل را بشناسد. کاربران معمولاً پورت، رمز، TLS و سابسکریپشن خودشان را نگه می‌دارند؛ فقط به‌جای آدرس خارج به آدرس رله وصل می‌شوند.

```text
users ──► Iranian relay :443 ══ authenticated, multiplexed tunnel ══► foreign exit ──► 127.0.0.1:443
             public address                  TLS / WSS / raw / UDP                 your panel or service
```

> **نکتهٔ بهره‌برداری:** فیلترینگ و مسیریابی در ایران بر اساس اپراتور، استان و زمان فرق می‌کند. سیمرغ چند حامل، مسیر جایگزین و ابزار اندازه‌گیری دارد؛ هیچ تنظیمی نمی‌تواند اتصال را هنگام قطعی تضمین یا عبور از هر مسیر را وعده کند.

## چرا سیمرغ؟

| قابلیت | نتیجه برای شما |
|---|---|
| **فوروارد شفاف** | مپینگ TCP و UDP، حالت اختیاری SOCKS5/HTTP، پشتیبانی PROXY protocol و سقف نرخ/کواتای هر پورت. |
| **مناسب مسیر ناپایدار** | کنترل جریان اعتباری، پنجرهٔ خودکار استریم، اتصال مجدد، استخر تونل و چند نقطهٔ اگزیت. |
| **چند قالب روی سیم** | TLS، WebSocket امن، حامل خام رمزشده، UDP با ارسال مجدد/FEC و plain برای شبکهٔ مورداعتماد. |
| **دو موتور سازگار** | موتور کامپایل‌شدهٔ Go و پیاده‌سازی قابل‌حمل Python 3.9+ می‌توانند دو سر یک تونل باشند. |
| **قابل بهره‌برداری، نه فقط نصب** | لینک جفت‌سازی، مدیریت زندهٔ پورت، تغییر با بکاپ/بازگردانی، doctor، تست سرعت، لاگ، هشدار و متریک Prometheus. |
| **خودکفا** | بدون دیتابیس، Node، CDN، فونت خارجی یا سرویس جدا برای فایل‌های پنل؛ خوانندهٔ TOML برای Python 3.9/3.10 همراه پروژه است. |

## معماری

**حالت مستقیم (پیش‌فرض):** رله به اگزیت وصل می‌شود. پورت تونل انتخاب‌شده باید روی سرور خارج باز باشد.

```text
Iran relay ──dial──► foreign exit :443
Iran relay :8443 ──user traffic──► exit target 127.0.0.1:8443
```

**حالت معکوس (`dial = "exit"`):** اگزیت به رله وصل می‌شود. برای وقتی مناسب است که سرور خارج اتصال ورودی نمی‌پذیرد یا ترجیح می‌دهید شنوندهٔ تونل در ایران باشد. در هر دو حالت، پورت‌های کاربران روی رله می‌مانند.

<a id="quick-start"></a>
## شروع سریع

### پیش‌نیازها

- یک سرور لینوکسی برای هر نقش و دسترسی root/sudo برای نصاب یک‌خطی.
- Python **۳.۹ یا جدیدتر**. در صورت وجود `systemd` سرویس ساخته می‌شود؛ اجرای دستی و کانتینر هم پشتیبانی می‌شود.
- یک پورت قابل‌دسترس برای تونل و برای TLS عملیاتی، fingerprint گواهی یا گواهی معتبر CA.

### ۱ — نصب اگزیت خارج

تعیین صریح نقش باعث می‌شود نصب به تشخیص جغرافیایی IP وابسته نباشد:

```bash
curl -fsSL https://raw.githubusercontent.com/Dark-Sky07/Simurgh-Tunnel/main/install.sh \
  | sudo bash /dev/stdin --role exit --name de-frankfurt
```

توکن و لینک راه‌اندازی ساخته‌شده را محرمانه نگه دارید. برای انتخاب پورت‌های شنوندهٔ تونل:

```bash
curl -fsSL https://raw.githubusercontent.com/Dark-Sky07/Simurgh-Tunnel/main/install.sh \
  | sudo bash /dev/stdin --role exit --listen 443,2053 --name de-frankfurt
```

### ۲ — نصب رله ایران

رله را می‌توان پیش از جفت‌سازی نصب کرد. پنل در وضعیت **جفت‌نشده** بالا می‌آید؛ نصب به‌دلیل نبود آدرس اگزیت شکست نمی‌خورد.

```bash
curl -fsSL https://raw.githubusercontent.com/Dark-Sky07/Simurgh-Tunnel/main/install.sh \
  | sudo bash /dev/stdin --role relay --name ir-tehran
```

اگر `--role` را حذف کنید، نصاب نقش را می‌پرسد یا خودش تشخیص می‌دهد. اجرای دوبارهٔ نصاب، چک‌اوت موجود سورس را پیش از صدا زدن CLI تازه می‌کند تا نصاب جدید با CLI قدیمیِ کش‌شده مخلوط نشود.

### ۳ — جفت‌سازی، فوروارد و بررسی

```bash
# روی اگزیت: فقط لینک راه‌اندازی را چاپ کنید
sudo simurgh link --show

# روی رله: لینک کامل را داخل کوتیشن بگذارید
sudo simurgh join 'simurgh://<exit-ip>:8787/setup?u=admin&p=…#de-frankfurt'

# در صورت نیاز یک مپینگ دیگر بسازید (join پورت‌های اعلام‌شدهٔ اگزیت را وارد می‌کند)
sudo simurgh mapping add 8443 8443 --name my-service
sudo simurgh restart

sudo simurgh status --detail
sudo simurgh doctor
```

می‌توانید لینک را در **پنل ← جفت‌سازی** هم بچسبانید. جفت‌سازی تنظیمات پنل، موتور و مپینگ‌های فعلی رله را حفظ می‌کند.

<details>
<summary><strong>نصب مستقیم بدون لینک راه‌اندازی</strong></summary>

اگر توکن و اعتبارسنجی گواهی را خودتان مدیریت می‌کنید، نصب یک‌مرحله‌ای هم ممکن است:

```bash
curl -fsSL https://raw.githubusercontent.com/Dark-Sky07/Simurgh-Tunnel/main/install.sh \
  | sudo bash /dev/stdin --role relay \
      --exit 203.0.113.9:443 --token 'SHARED-TOKEN' --fingerprint 'SHA256-FINGERPRINT'
```

روی اگزیت `simurgh cert` را اجرا کنید تا fingerprint را ببینید. گواهی پیش‌فرض اگزیت خودامضا است: **توکن و آدرس به‌تنهایی هویت آن را احراز نمی‌کنند**. از لینک راه‌اندازی، fingerprint یا گواهی معتبر CA استفاده کنید. `--insecure` فقط برای آزمون موقت است.

</details>

### بسته‌ها و کانتینر

فایل‌های هر ریلیز شامل `.deb`، `.rpm`، `.tar.gz` قابل‌حمل و موتورهای کامپایل‌شده است. `Dockerfile` و `docker-compose.yml` هم در مخزن هستند.

```bash
sudo dpkg -i simurgh_3.1.1_amd64.deb
sudo dnf install ./simurgh-3.1.1-1.x86_64.rpm
sudo tar -xzf simurgh-3.1.1-noarch.tar.gz -C /
docker compose up -d
```

برای نصب آفلاین، محتوای بسته و ارتقا: **[بسته‌ها و کانتینر](docs/PACKAGING.fa.md)**.

<a id="iran-playbook"></a>
## راهنمای بهره‌برداری در مسیرهای ایران

محافظه‌کار شروع کنید، روی همان مسیر واقعی رله ← اگزیت اندازه بگیرید و هر بار فقط یک متغیر را تغییر دهید.

| وضعیت | روش پیشنهادی |
|---|---|
| **شروع عملیاتی معمول** | `tls` روی TCP 443 با fingerprint گواهی، `split = 3` و یک اتصال تونل. پایه را با `status`، `doctor` و `speedtest` تأیید کنید. |
| **ریست TLS یا دخالت در بستهٔ اول** | مقدارهای `split = 0`، `1` و `3` را جدا آزمایش کنید. تکه‌کردن یک heuristic برای سازگاری است، نه تضمین؛ آزمون کنترل‌شدهٔ بعدی `wss` است. |
| **مسیر با فلوهای WebSocket/HTTPS بهتر کار می‌کند** | `wss` را با مسیری معمول مثل `/ws` و همان توکن و اعتبارسنجی گواهی امتحان کنید. |
| **RTT بالا یا محدودیت یک فلوی TCP** | ابتدا `connections = 2` و سپس `4` را بسنجید؛ علاوه بر توان عبوری، تأخیر زیر بار را هم مقایسه کنید. اتصال بیشتر همیشه بهتر نیست. |
| **افت دیتاگرام روی مسیر تونل** | `udp` را فقط بعد از اطمینان از عبور UDP در همان مسیر انتخاب کنید؛ `speedtest --udp` بگیرید و تنظیمات FEC مستند را ارزیابی کنید. حامل TCP را به‌عنوان جایگزین نگه دارید. |
| **ورودی سرور خارج در دسترس نیست** | حالت معکوس را انتخاب و شنوندهٔ تونل را روی رله باز کنید. |
| **اگزیت یا مسیر ناپایدار شده** | چند نقطهٔ اگزیت/شنونده تنظیم و سلامت استخر را در پنل ببینید. UDP یا splitting تهاجمی را روی همه‌چیز تحمیل نکنید. |

حامل `raw` روی موتور Python به بستهٔ `cryptography` نیاز دارد و نصاب تلاش می‌کند آن را فراهم کند. `plain` لایهٔ بیرونی TLS ندارد و برای مسیر مورداعتماد/داخلی است، نه پیش‌فرض اینترنت عمومی.

جزئیات بیشتر: **[تنظیمات](docs/CONFIGURATION.fa.md)** و **[عیب‌یابی](docs/TROUBLESHOOTING.fa.md)**.

## فرمان‌های روزمره

```bash
simurgh menu                       # منوی تعاملی اپراتور
simurgh status --detail            # سرویس، جفت‌سازی، مپینگ و وضعیت تونل
simurgh doctor                     # بررسی تنظیمات و اتصال
simurgh mapping list               # فهرست فورواردهای کاربران
simurgh mapping add 443 443        # پورت رله -> پورت مقصد روی اگزیت
simurgh speedtest --udp            # توان عبوری، تأخیر زیر بار و افت UDP
simurgh logs -f                    # دنبال‌کردن لاگ نقش فعال
simurgh tune balanced              # بازگشت به پروفایل پایه
simurgh update --check             # فقط بررسی، بدون تغییر نصب
simurgh link --show                # لینک راه‌اندازی برای سرور دیگر
simurgh start | stop | restart     # کنترل سرویس
```

برای گزینه‌های هر فرمان: `simurgh <command> --help`.

<a id="web-panel"></a>
## پنل وب

آدرس `http://<relay-ip>:8787` (یا پورت تنظیم‌شدهٔ پنل) را باز کنید. پنل **فارسی / English** است، چیدمان RTL/LTR و تم night/dawn دارد و همهٔ فایل‌ها را با CSP سخت از خودش سرو می‌کند.

- **داشبورد:** نقشهٔ زندهٔ تونل، نمودار سرعت، اتصال‌های مجدد، RTT و سلامت استخر اگزیت.
- **جفت‌سازی:** تکمیل رلهٔ جفت‌نشده بدون نصب دوباره.
- **پورت‌ها:** افزودن، حذف و روشن/خاموش‌کردن مپینگ‌ها بدون ری‌استارت نود زنده.
- **ویزارد:** پیش‌نمایش تغییر، اعمال با بکاپ و امکان بازگردانی.
- **بهره‌برداری:** تست سرعت، گواهی، لاگ قابل‌فیلتر، ابزار سرویس و بررسی به‌روزرسانی.
- **امنیت:** وضعیت ورود ناموفق و لینک یک‌بارمصرف؛ پنج تلاش اشتباه از یک آدرس، قفل ۳۰۰‌ثانیه‌ای ایجاد می‌کند.
- **مانیتورینگ:** `/metrics` با احراز هویت و `/healthz` فقط برای وضعیت حیات.

فرم ورود یا **لینک یک‌بارمصرف ۱۰‌دقیقه‌ای** صفحهٔ **امنیت** / `simurgh web --link` را ترجیح دهید. لینک قدیمی `?k=<password>` بعد از ساخت کوکی به آدرس تمیز هدایت می‌شود.

> پنل داخلی از HTTP استفاده می‌کند. روی سرور عمومی، آدرس‌های مجاز را با فایروال محدود کنید یا تونل SSH بسازید: `ssh -L 8787:127.0.0.1:8787 root@server` و سپس `http://127.0.0.1:8787`.

مرجع کامل: **[راهنمای پنل](docs/PANEL.fa.md)**.

## موتورها و کارایی

| موتور | کاربرد مناسب |
|---|---|
| **Go** | دیتاپلین پرسرعت و چند‌هسته‌ای؛ انتخاب اول وقتی باینری مناسب موجود است. |
| **Python** | جایگزین قابل‌حمل و مرجع پروتکل؛ اجرای TLS/WSS/plain به وابستگی third-party نیاز ندارد. |

هر دو موتور یک کانفیگ را می‌خوانند و با هم کار می‌کنند. با `simurgh engine go` یا `simurgh engine python` و سپس restart تعویضشان کنید. سرعت واقعی به CPU، RTT، افت، حامل، تعداد فلو و سرویس مقصد بستگی دارد، نه فقط موتور.

به‌جای برآورد نتیجهٔ اینترنت از بنچمارک لوپ‌بک، روی مسیر خودتان اندازه بگیرید:

```bash
python tools/live_smoke.py
python tools/latency_bench.py 60 40 tls --connections 4
simurgh speedtest --streams 4 --udp
```

<a id="security"></a>
## امنیت

- hello نسخه‌دار و زمان‌دار با **HMAC-SHA-256** و محافظت replay.
- **fingerprint SHA-256** برای TLS خودامضا و پشتیبانی اعتبارسنجی CA. در محیط عملیاتی `insecure_skip_verify` را فعال نکنید.
- رمزنگاری AEAD برای حامل‌های raw/UDP؛ SHA-1 در WebSocket فقط همان‌جایی است که RFC 6455 لازم دارد و برای هش رمز استفاده نمی‌شود.
- نوشتن اتمیک کانفیگ با سطح دسترسی `0600` و ماسک‌کردن توکن در نماهای پنل/کانفیگ.
- احراز هویت پنل، قفل per-IP، کوکی `HttpOnly`/`SameSite`، توکن ورود یک‌بارمصرف، CSP سخت و نبود asset شخص ثالث.
- باینری به‌روزرسانی **فقط با وجود و تطبیق چک‌سام SHA-256** نصب می‌شود و نسخهٔ قبلی با پسوند `.bak` می‌ماند.

تونل جای امن‌سازی پنل مقصد را نمی‌گیرد. سرویس‌ها را به‌روز نگه دارید، رابط‌های مدیریتی را محدود کنید و با لینک راه‌اندازی مثل رمز رفتار کنید.

## آزمون و توسعه

```bash
git clone https://github.com/Dark-Sky07/Simurgh-Tunnel.git
cd Simurgh-Tunnel
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[secure]' pytest ruff

python -m pytest -q
ruff check --select F,E9 simurgh tests
python tools/live_smoke.py

# موتور Go (Go 1.27+؛ سورس third-party همراه پروژه است)
cd go
GOPROXY=off go test ./...
GOPROXY=off go test -race ./...
```

تست‌ها پروتکل/mux، حامل‌ها، جفت‌سازی مستقیم و معکوس، reconnect، UDP، سقف و کواتا، regressionهای نصاب، کانفیگ، API و JavaScript پنل، بسته‌بندی و سازگاری Python ↔ Go را پوشش می‌دهند. تست‌های وابسته به ابزار بسته‌بندی یا Go در نبود ابزار به‌صورت کنترل‌شده skip می‌شوند.

<a id="documentation"></a>
## مستندات

| راهنما | محتوا |
|---|---|
| [تنظیمات](docs/CONFIGURATION.fa.md) | همهٔ کلیدها، حامل‌ها، مپینگ، استخر، پروفایل کارایی و systemd |
| [پنل](docs/PANEL.fa.md) | نماها، احراز هویت، API، امنیت و مانیتورینگ |
| [بسته‌ها](docs/PACKAGING.fa.md) | deb/rpm/tarball، Docker، نصب آفلاین و ارتقا |
| [عیب‌یابی](docs/TROUBLESHOOTING.fa.md) | جفت‌سازی، گواهی، فیلترینگ، پورت، کارایی و لاگ |
| [مقایسه](docs/COMPARISON.fa.md) | مقایسهٔ فنی با روش‌های مشابه |

نسخهٔ انگلیسی هر راهنما از بالای همان صفحه لینک شده است.

## مجوز

MIT — فایل [LICENSE](LICENSE). مجوز سورس‌های third-party همراه پروژه، از جمله `simurgh/_tomli/` و `go/third_party/`، کنار همان سورس نگه داشته شده است.

</div>
