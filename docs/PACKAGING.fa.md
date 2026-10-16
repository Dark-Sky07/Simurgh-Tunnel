# بسته‌ها و کانتینر

[English](PACKAGING.md) · **فارسی**

<div dir="rtl">

`install.sh` همچنان کوتاه‌ترین راه است (روی هر توزیع systemd کار می‌کند و موتور
کامپایل‌شده را هم برایتان می‌سازد). بسته‌ها برای ماشین‌هایی هستند که در آن‌ها
package manager یا ایمیج یا «باز کن و اجرا کن» جواب درست است — و برای کسانی که
اصلاً نمی‌توانند اسکریپتی از اینترنت اجرا کنند.

| | فرمان | پوشش |
|---|---|---|
| **یک فرمان** | `curl … \| sudo bash` | همه‌چیز، همراه ساخت موتور Go |
| **`.deb`** | `sudo dpkg -i simurgh_2.9.0_amd64.deb` | دبیان، اوبونتو، proxmox |
| **`.rpm`** | `sudo dnf install ./simurgh-2.9.0-1.x86_64.rpm` | ردهت، فدورا، آلما، راکی، openSUSE |
| **tarball** | `tar -xzf simurgh-2.9.0-noarch.tar.gz -C /` | هر جایی که پایتون ۳٫۹+ دارد |
| **کانتینر** | `docker compose up -d` | هر دو نقش روی یک ماشین، یا یک نقش روی هر سرور |

## داخل بسته چه چیزی است

بسته هرگز به کانفیگ شما دست نمی‌زند: `/etc/simurgh` را با مجوز `0700` می‌سازد و
همین. بقیه دقیقاً همان فایل‌هایی است که `install.sh` می‌گذارد:

| مسیر | چیست |
|---|---|
| `/usr/bin/simurgh` | CLI، منو، پنل و ناظر هر دو موتور |
| `/usr/lib/simurgh/simurgh/` | موتور پایتون (بدون وابستگی: خوانندهٔ TOML همراه خودش است) |
| `/usr/bin/simurgh-go` | موتور کامپایل‌شده، در بسته‌هایی که آن را دارند (هر دو حالت `.deb`/`.rpm` و tarball معماری شما) |
| `/usr/lib/systemd/system/simurgh-{relay,exit}.service` | یک unit برای هر نقش؛ بسته فقط نقشی را فعال می‌کند که **کانفیگش** از قبل موجود باشد |
| `/usr/share/doc/simurgh/` | همین فایل، README هر دو زبان و مرجع تنظیمات |

دو حالت بسته عمدی است: `simurgh_2.9.0_all.deb` / `simurgh-2.9.0-1.noarch.rpm`
خالص پایتون‌اند و همه‌جا اجرا می‌شوند، ولی `simurgh_2.9.0_amd64.deb` و
`…-1.x86_64.rpm` موتور کامپایل‌شده را هم دارند — که CLI خودش از
`/usr/bin/simurgh-go` برمی‌دارد، پس `engine = "go"` بدون تنظیم هیچ مسیری کار
می‌کند.

## ساختن بسته‌ها

```bash
bash packaging/build-packages.sh                              # .deb + .tar.gz این‌جا
bash packaging/build-packages.sh --out dist                   # داخل dist/
bash packaging/build-packages.sh --out dist \
     --go-binary dist/simurgh-go-linux-amd64                  # با موتور Go
bash packaging/build-packages.sh --out dist --prefix /opt/simurgh
```

بدون شبکه و بدون ابزار بسته‌بندی پایتون: درخت در یک ریشهٔ موقت کپی می‌شود،
مسیر کتابخانه در launcher جای‌گذاری می‌شود و `dpkg-deb` / `rpmbuild` (هر کدام که
نصب باشد) از آن بسته می‌سازد. با `--no-deb`، `--no-rpm` و `--no-tar` هر کدام را
نخواهید رد می‌کنید، و روی ماشینی که هیچ‌کدام از آن دو ابزار را ندارد باز هم
tarball را می‌گیرید.

CI روی هر push هر چهار فایل را می‌سازد (کار `packages` در
`.github/workflows/ci.yml`) و workflow ریلیز آن‌ها را کنار شش باینری موتور به
ریلیز گیت‌هاب پیوست می‌کند.

## نصب

### دبیان، اوبونتو، proxmox

```bash
sudo dpkg -i simurgh_2.9.0_amd64.deb      # اگر وابستگی کم بود: apt -f install
sudo simurgh install --role exit --listen 443      # یا --role relay --exit-host <ip>
sudo systemctl enable --now simurgh-exit           # unit همان نقشی که ساختید
```

`remove` پکیج، `/etc/simurgh` را نگه می‌دارد (توکن‌ها و دفتر ترافیک از نصب دوباره
جان سالم به در می‌برند) و `purge` آن را هم پاک می‌کند. پنل، متریک‌ها، هشدارها،
کوتاها و `simurgh update` دقیقاً مثل نصب با `install.sh` کار می‌کنند.

### ردهت، فدورا، آلما، راکی، openSUSE

```bash
sudo dnf install ./simurgh-2.9.0-1.x86_64.rpm
sudo simurgh install --role relay --exit-host 203.0.113.9 --exit-port 443 --token XXX
sudo systemctl enable --now simurgh-relay
```

### هر جای دیگر: tarball

ریشه‌اش `/` است، پس روی هر سیستمی که پایتون ۳٫۹+ دارد باز می‌شود:

```bash
sudo tar -xzf simurgh-2.9.0-noarch.tar.gz -C /
sudo simurgh install --role exit --listen 443
sudo simurgh exit          # بدون systemd؟ نقش را دستی اجرا کنید
```

پیشوندی جز `/usr` فقط یک سوییچ در زمان ساخت است (`--prefix /opt/simurgh`) و
launcher آن‌وقت در `/opt/simurgh/lib/simurgh` را نگاه می‌کند (متغیر
`$SIMURGH_LIB` در زمان اجرا آن را عوض می‌کند؛ همین باعث می‌شود درختی که جای
دیگری باز شده باشد هم اجرا شود).

## کانتینر

```bash
docker build -t simurgh .
docker run --rm simurgh --version
docker run --rm -v simurgh-exit:/etc/simurgh simurgh init --role exit --listen 443
docker run -d --name simurgh-exit -v simurgh-exit:/etc/simurgh \
       -p 443:443 -p 19443:19443 simurgh exit
```

ایمیج دو مرحله‌ای است: موتور کامپایل‌شده از همین درخت با `GOPROXY=off` ساخته
می‌شود (همهٔ وابستگی‌های Go به‌صورت vendor داخل مخزن‌اند) و مرحلهٔ اجرا
`python:3.12-slim` با `ca-certificates` و `curl` است — سمت پایتون چیزی برای نصب
ندارد. هیچ کانفیگی داخلش نیست؛ `/etc/simurgh` یک والیوم است و فقط خودِ
`simurgh` در آن می‌نویسد، دقیقاً مثل روی سرور.

فایل `docker-compose.yml` یک رله و یک اگزیت را کنار هم بالا می‌آورد؛ سریع‌ترین
راه برای دیدن کارکرد کل سیستم:

```bash
docker compose build
TOKEN=$(openssl rand -hex 24)
docker compose run --rm exit  install --role exit  --listen 19443 --token "$TOKEN" --force
docker compose run --rm relay install --role relay --exit-host exit --exit-port 19443 \
    --carrier tls --insecure --mapping 8443:9000 --token "$TOKEN" --force
docker compose up -d
docker compose run --rm relay status        # و پنل روی :8787
```

`--insecure` فقط برای همین جفت محلی است (اگزیت گواهی خودامضا سرو می‌کند)؛ روی دو
سرور واقعی گواهی را پین کنید (`--fingerprint AB:CD:…`) یا با
`simurgh cert issue` گواهی واقعی بگیرید.

روی سرورهای واقعی از شبکهٔ میزبان استفاده کنید تا تونل رابط‌ها و پورت‌های واقعی
ماشین را ببیند (نمونهٔ دو سرویسی در پایین همان فایل compose هست):

```yaml
services:
  relay:
    image: ghcr.io/dark-sky07/simurgh-tunnel:latest
    network_mode: host
    command: ["relay"]
    volumes: ["/etc/simurgh:/etc/simurgh"]
    restart: unless-stopped
```

entrypoint اگر نقشی کانفیگ نداشته باشد اجازهٔ بالا آمدن نمی‌دهد و می‌گوید چه
فرمانی بزنید — کانتینر همان CLI است، پس `docker compose exec relay simurgh menu`
یا `… simurgh web` مثل همیشه کار می‌کنند.

## نکته‌ها

* **به‌روزرسانی.** `simurgh update` هر سه نوع نصب را می‌شناسد: کپی git به آخرین
  تگ می‌رود، نصب بسته‌ای فایل مناسب همین ماشین را از ریلیز دانلود می‌کند (با
  بررسی SHA-256) و نصب pip از مسیر pip به‌روز می‌شود. `apt upgrade` و
  `dnf upgrade` هم برای بسته‌ها کار می‌کنند.
* **`simurgh doctor`** بعد از هر روش نصبی همان بررسی‌ها را انجام می‌دهد و
  `simurgh status` به‌عنوان healthcheck کانتینر مناسب است
  (`simurgh status --json` وقتی کانفیگ خوانده شود کد ۰ می‌دهد).
* **موتور Go در کانتینر** در `/usr/bin/simurgh-go` است و خودکار استفاده می‌شود؛
  همان `engine = "go"` در کانفیگ کافی است.
* **هیچ بسته‌ای کانفیگ نمی‌سازد**: دو سرور با یک توکن جفت می‌شوند که به هر دو
  می‌دهید (`--token`)، یا با یک لینک اتصال (`simurgh link --show` →
  `simurgh join '<link>'`).

</div>
