"""Connection profiles offered by the bot.

All profiles use the same wire protocol (KILLDPI over WebSocket+TLS through
Cloudflare) because the server runs inside GitHub Actions and is only
reachable through a Cloudflare quick tunnel. The profiles differ in cipher,
handshake padding and keep-alive; none of them can guarantee undetectability.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    key: str
    title: str
    emoji: str
    summary: str
    description: str
    limitations: str
    cipher: str
    padding: str      # "min-max" handshake padding bytes
    keepalive: int    # seconds, 0 = off


PROFILES: dict[str, Profile] = {
    "strong_dpi": Profile(
        key="strong_dpi", title="DPI قوی", emoji="🛡",
        summary="شرایط دشوار؛ پدینگ زیاد، ChaCha20",
        description=(
            "ترافیک KILLDPI داخل WebSocket روی TLS 1.3 و از طریق Cloudflare عبور می‌کند. "
            "اثرانگشت TLS کروم (uTLS) استفاده می‌شود و هندشیک با پدینگ تصادفی بزرگ (256–1024 بایت) "
            "پوشانده می‌شود. در صورت پروب فعال، سرور به یک وب‌سایت واقعی fallback می‌کند."),
        limitations=(
            "• تأخیر کمی بالاتر (پدینگ بزرگ‌تر)\n"
            "• سرعت به ظرفیت Cloudflare و رانر GitHub محدود است\n"
            "• هیچ روشی شناسایی‌ناپذیریِ کامل را تضمین نمی‌کند"),
        cipher="chacha20-poly1305", padding="256-1024", keepalive=25),
    "gaming": Profile(
        key="gaming", title="Gaming", emoji="🎮",
        summary="کمترین سربار؛ AES-GCM، پدینگ کم",
        description=(
            "هر دیتاگرام UDP بازی داخل یک فریم رمزشده با مرز حفظ‌شده حمل می‌شود. "
            "پدینگ کم و keep-alive کوتاه برای پایداری NAT. AES-256-GCM روی CPUهای با شتاب‌دهنده AES سریع‌تر است."),
        limitations=(
            "• مسیر از Cloudflare می‌گذرد؛ پینگ وابسته به نزدیک‌ترین PoP است\n"
            "• UDP روی TCP حمل می‌شود؛ در شبکه با packet loss بالا ممکن است head-of-line blocking رخ دهد\n"
            "• پدینگ کم = مقاومت کمتر در برابر تحلیل آماری"),
        cipher="aes-256-gcm", padding="16-64", keepalive=15),
    "web": Profile(
        key="web", title="Web Browsing", emoji="🌐",
        summary="وب‌گردی روزمره؛ متعادل",
        description="پدینگ متوسط و keep-alive معمولی؛ مناسب مرورگر و اپ‌های روزمره.",
        limitations=(
            "• برای استریم سنگین یا دانلود بزرگ، سرعت به CDN وابسته است"),
        cipher="chacha20-poly1305", padding="64-512", keepalive=30),
    "general": Profile(
        key="general", title="General", emoji="⚖️",
        summary="پیش‌فرض؛ گزینهٔ خوب برای شروع",
        description="پدینگ پیش‌فرض (64–512)، ChaCha20-Poly1305 و keep-alive ۳۰ ثانیه.",
        limitations=(
            "• اگر شبکهٔ شما IP های Cloudflare را محدود می‌کند، از لیست IP تمیز استفاده کنید"),
        cipher="chacha20-poly1305", padding="64-512", keepalive=30),
}

PROFILE_ORDER = ["strong_dpi", "gaming", "web", "general"]
