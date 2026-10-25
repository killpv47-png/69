# KILLDPI-bot

ربات تلگرام KILLDPI که **خودش سرور KILLDPI را داخل GitHub Actions اجرا می‌کند** و کانفیگ می‌دهد.
هیچ تغییری در پروتکل داده نشده؛ همان هستهٔ Xray از ریلیزهای ریپوی [KILLDPI](https://github.com/fjxjxjjdjddjjd042-rgb/KILLDPI) (تگ `core-build-*`) دانلود و اجرا می‌شود.

## معماری

```
GitHub Actions run (~5h40m)
 ├─ bin/xray  ──  killdpi inbound (WS, 127.0.0.1:8080) + gRPC API (:10085) + fallback → decoy (:8081)
 ├─ bin/cloudflared  quick tunnel  https://<random>.trycloudflare.com → 127.0.0.1:8080
 └─ bot/bot.py  Telegram long-polling
        ├─ «کانفیگ جدید» → UUID می‌سازد → همان لحظه با gRPC روی xray فعال می‌کند → لینک می‌دهد
        ├─ ساب هر کاربر = یک gist مخفی (URL ثابت) که محتوایش دائم بازنویسی می‌شود
        ├─ هر ۱۰ دقیقه نام آیتم اول ساب (⏳ تا بروزرسانی: HHh MMm) آپدیت می‌شود
        ├─ state در data/state.json همین ریپو (Contents API، با SHA → بدون تداخل)
        └─ ۱۰ دقیقه قبل از پایان: workflow_dispatch ران بعدی → ران بعدی تونل جدید می‌سازد
           و همهٔ ساب‌ها را با آدرس جدید بازنشر می‌کند → ران قبلی خاموش می‌شود (hand-over)
```

کلاینت‌ها به **IP تمیز Cloudflare** با `SNI/Host = <tunnel>.trycloudflare.com` وصل می‌شوند (یا مستقیم به خود تونل). هر کانفیگ در ساب با چند آدرس و نام متفاوت می‌آید: `CF-Auto`, `Clean-1 188.114.96.1`, … .

## چرا GitHub Gist برای ساب؟
URL خام gist (`https://gist.githubusercontent.com/<user>/<id>/raw/<file>`) ثابت است و با هر `PATCH` محتوای جدید می‌دهد (کش CDN ≤ ۵ دقیقه). پس لینک ساب هرگز عوض نمی‌شود، فقط آدرس داخل آن.

## راه‌اندازی (یک بار)

1. **Secrets** (Settings → Secrets and variables → Actions → Secrets):
   - `BOT_TOKEN` – توکن ربات از @BotFather
   - `GH_PAT` – PAT با scopeهای `repo`, `gist`, `workflow`
   - `KILLDPI_PRIVATE_KEY` – لازم نیست بسازید؛ ربات در اولین ران خودش می‌سازد و ذخیره می‌کند (تا کلید عمومی و کانفیگ‌ها ثابت بمانند)
2. **Variables** (اختیاری): `ADMIN_IDS=123,456` ، `BOT_ENABLED=false` برای توقف، `RUN_SECONDS`, `CORE_REPO`
3. Actions → **killdpi-bot** → Run workflow. از این به بعد خودش ادامه می‌دهد.

## دستورات ربات

| کاربر | ادمین |
|---|---|
| `/start` `/new` `/mine` `/sub` `/status` `/ping` `/apps` `/help` | `/admin` `/cleanips` `/maxips` `/users` `/broadcast` `/relaunch` `/maintenance` `/export` `/log` |

همهٔ عملیات هم با **کامند**، هم با **کیبورد پایین** (Reply Keyboard) و هم با **دکمه‌های داخل پیام** (Inline) در دسترس است.

## ساختار

```
bot/bot.py           ربات (منوها، ساخت/حذف کانفیگ، پنل ادمین، حلقهٔ اصلی و hand-over)
bot/server.py        اجرای xray + cloudflared + decoy داخل رانر
bot/xray_api.py      کلاینت gRPC دست‌ساز برای HandlerService/StatsService (adu رسمی KILLDPI را نمی‌شناسد)
bot/subscription.py  ساخت ساب: شمارش معکوس، چند آدرس برای هر کانفیگ، IP تمیز
bot/storage.py       GitHub Contents API (state) + Gist (ساب) + Secrets + workflow_dispatch
bot/killdpi_link.py  فرمت لینک killdpi:// (بدون تغییر از ریپوی اصلی؛ فقط geoip حذف شد)
bot/profiles.py      پروفایل‌ها (DPI قوی / Gaming / Web / General) — فقط cipher/padding/keepalive فرق دارند
scripts/fetch_bins.sh دانلود هسته از ریلیز KILLDPI + cloudflared
tests/test_offline.py  تست‌های آفلاین (در هر ران هم اجرا می‌شود)
tests/test_live_server.py  تست واقعی: سرور + تونل + کلاینت از روی لینک ساب
.github/workflows/bot.yml
data/state.json      دیتابیس (توسط ربات commit می‌شود)
```

## تست

```bash
pip install -r bot/requirements.txt
bash scripts/fetch_bins.sh fjxjxjjdjddjjd042-rgb/KILLDPI bin
(cd bot && python ../tests/test_offline.py)
python tests/test_live_server.py          # اینترنت لازم دارد
LOCAL_STORAGE=1 BOT_TOKEN=... python bot/bot.py   # اجرای محلی با سرور واقعی
```

## محدودیت‌ها (صادقانه)
- هر ران GitHub حداکثر ۶ ساعت است؛ در لحظهٔ جابه‌جایی چند ثانیه تا ~۱ دقیقه قطعی هست و کلاینت باید ساب را بروزرسانی کند (آیتم اول ساب زمان باقی‌مانده را نشان می‌دهد).
- سرعت به رانر GitHub و Cloudflare محدود است.
- هیچ پروتکلی شناسایی‌ناپذیریِ کامل را تضمین نمی‌کند.
