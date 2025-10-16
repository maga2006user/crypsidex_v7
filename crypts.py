# crypsidex_v7.py
# CrypSideX v7 — минималистичный бот: 2 кнопки (📊 Курсы, 🧠 Инсайды)
# - собирает RSS новости (мировые финансы, крипто, экономика, политика)
# - переводит заголовки на русский
# - делает топ-5 релевантных инсайдов
# - простая "AI-аналитика" (эвристическая): темы, риски, краткие рекомендации
#
# Установка зависимостей:
# pip install pyTelegramBotAPI requests
#
# Перед запуском: вставь свой токен в переменную TOKEN

import telebot
from telebot import types
import requests
import xml.etree.ElementTree as ET
import threading
import time
import random
from datetime import datetime

# ----------------- НАСТРОЙКИ -----------------
TOKEN = "8237181059:AAFnyfGwdzeQX-ZrQkagn56JIpSb12YgAVI"   # <-- Вставь токен
UPDATE_INTERVAL = 8 * 60    # сек: как часто обновлять кеш (8 минут)
CACHE = {
    "usd": None,
    "btc": None,
    "gold": None,
    "news": [],       # список кортежей (источник, оригинал, перевод)
    "updated": None
}
USER_AGENT = "CrypSideXBot/1.0 (+https://t.me/your_channel)"
session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# ----------------- ИСТОЧНИКИ -----------------
RSS_FEEDS = [
    ("Reuters", "https://www.reuters.com/rssFeed/businessNews"),
    ("Bloomberg", "https://www.bloomberg.com/feed/podcast/etf-report.xml"),  # может не всегда иметь теги <item>
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("FT", "https://www.ft.com/?format=rss"),
    ("Investing", "https://www.investing.com/rss/news.rss"),
]

MARKET_KEYWORDS = [
    "tariff", "trade", "sanction", "inflation", "rate", "fed", "cpi", "gdp",
    "bank", "default", "hack", "regulation", "etf", "ipo", "halving", "merger",
    "acquisition", "crash", "collapse", "tariffs", "tariff", "tariff", "trade war"
]
PERSON_KEYWORDS = ["trump", "musk", "elon", "zuckerberg", "biden", "xi", "putin", "saylor", "cz", "binance"]

# ----------------- УТИЛИТЫ -----------------
def safe_get(url, timeout=7, params=None):
    try:
        r = session.get(url, timeout=timeout, params=params)
        if r.status_code == 200:
            return r
    except Exception:
        return None
    return None

def translate_to_ru(text):
    # Попытка LibreTranslate -> MyMemory -> оригинал
    try:
        r = session.post("https://libretranslate.de/translate",
                         data={"q": text, "source": "en", "target": "ru", "format": "text"},
                         timeout=6)
        if r.ok:
            j = r.json()
            tr = j.get("translatedText")
            if tr:
                return tr
    except Exception:
        pass
    try:
        r = session.get("https://api.mymemory.translated.net/get", params={"q": text, "langpair": "en|ru"}, timeout=6)
        if r and r.status_code == 200:
            j = r.json()
            tr = j.get("responseData", {}).get("translatedText")
            if tr:
                return tr
    except Exception:
        pass
    return text  # fallback

# ----------------- ПОЛУЧЕНИЕ КУРСОВ -----------------
def fetch_usd_cbr():
    r = safe_get("https://www.cbr-xml-daily.ru/daily_json.js", timeout=6)
    if not r:
        return None
    try:
        return round(r.json()["Valute"]["USD"]["Value"], 2)
    except Exception:
        return None

def fetch_btc_binance():
    r = safe_get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=6)
    if not r:
        return None
    try:
        return round(float(r.json().get("price", 0)), 2)
    except Exception:
        return None

def fetch_gold():
    # несколько фоллбеков
    try:
        r = safe_get("https://data-asg.goldprice.org/dbXRates/USD", timeout=6)
        if r:
            j = r.json()
            items = j.get("items")
            if items and isinstance(items, list):
                p = items[0].get("xauPrice")
                if p:
                    return round(float(p), 2)
    except Exception:
        pass
    try:
        r = safe_get("https://query1.finance.yahoo.com/v7/finance/quote?symbols=GC=F", timeout=6)
        if r:
            res = r.json().get("quoteResponse", {}).get("result", [])
            if res:
                p = res[0].get("regularMarketPrice")
                if p:
                    return round(float(p), 2)
    except Exception:
        pass
    return None

# ----------------- НОВОСТИ: сбор и обработка -----------------
def fetch_rss_headlines(limit_per_feed=6):
    headlines = []
    for name, url in RSS_FEEDS:
        r = safe_get(url, timeout=8)
        if not r:
            continue
        try:
            root = ET.fromstring(r.content)
            items = root.findall(".//item")
            count = 0
            for it in items:
                if count >= limit_per_feed:
                    break
                t = it.find("title")
                if t is not None and t.text:
                    headlines.append((name, t.text.strip()))
                    count += 1
        except Exception:
            # если парсинг упал — пропускаем
            continue
    return headlines

def build_news_cache():
    raw = fetch_rss_headlines(limit_per_feed=5)
    dedup = []
    seen = set()
    for src, title in raw:
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        tr = translate_to_ru(title)
        dedup.append((src, title, tr))
        if len(dedup) >= 25:
            break
    return dedup

# ----------------- "AI" ЭВРИСТИЧЕСКИЙ АНАЛИЗ -----------------
def analyze_headlines(headlines, top_n=5):
    """
    headlines: list of (src, orig, ru)
    Возвращает:
      - top_insights: список строк (топ N инсайдов в виде "[Source] Перевод")
      - analysis_text: строка с кратким эвристическим анализом и рекомендациями
    """
    # ранжируем релевантность по вхождениям ключевых слов
    scored = []
    for src, orig, ru in headlines:
        score = 0
        lower = (orig + " " + ru).lower()
        for kw in MARKET_KEYWORDS:
            if kw in lower:
                score += 2
        for p in PERSON_KEYWORDS:
            if p in lower:
                score += 3
        # небольшая бонуc за слова "war", "conflict", "attack"
        for w in ("war","conflict","attack","invasion","sanction","tariff","hack"):
            if w in lower:
                score += 2
        scored.append((score, src, orig, ru))
    scored.sort(key=lambda x: -x[0])

    top = scored[:top_n]
    top_insights = [f"[{src}] {ru}" for score, src, orig, ru in top]

    # собрать общие темы
    theme_counts = {}
    person_counts = {}
    risk_flags = set()
    for score, src, orig, ru in scored:
        text = (orig + " " + ru).lower()
        for kw in MARKET_KEYWORDS:
            if kw in text:
                theme_counts[kw] = theme_counts.get(kw, 0) + 1
        for p in PERSON_KEYWORDS:
            if p in text:
                person_counts[p] = person_counts.get(p, 0) + 1
        for w in ("war","invasion","conflict","attack"):
            if w in text:
                risk_flags.add("geo")
        for w in ("hack","breach","leak"):
            if w in text:
                risk_flags.add("security")

    # формируем аналитическую часть (эвристика)
    themes_sorted = sorted(theme_counts.items(), key=lambda x: -x[1])
    people_sorted = sorted(person_counts.items(), key=lambda x: -x[1])

    # подготовка анализа
    analysis_lines = []
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    analysis_lines.append(f"⏱ Обновление: {ts}")
    if themes_sorted:
        top_theme = themes_sorted[0][0]
        analysis_lines.append(f"🔎 Топ тема в новостях: «{top_theme}» (часто встречается)")
    else:
        analysis_lines.append("🔎 Топ тема: неявная — смешанные новости")

    if people_sorted:
        p_list = ", ".join([p.upper() for p, c in people_sorted[:3]])
        analysis_lines.append(f"👥 Персоны, которых упоминают чаще: {p_list}")

    # риски и сценарии
    if "geo" in risk_flags:
        analysis_lines.append("⚠️ Риск геополитической эскалации — может поднять волатильность на сырьевых рынках и валюте.")
    if "security" in risk_flags:
        analysis_lines.append("🔐 Риски безопасности (взломы/утечки) — потенциальный удар по биржам/ликвидности.")

    # простые рекомендации (эвристика по сочетанию тем)
    rec = []
    # если много упоминаний про tariffs/sanction -> возможно торговые войны
    if any(k in dict(themes_sorted) for k in ("tariff","sanction","trade")):
        rec.append("Пересмотреть экспозицию на импортозависимые активы; подумать о хеджах против пошлин.")
    # если много про rate/fed/inflation
    if any(k in dict(themes_sorted) for k in ("rate","fed","inflation","cpi")):
        rec.append("Долгосрочные облигации и чувствительные к ставкам активы — под угрозой; рассмотрите защиту портфеля.")
    # если про hack/security
    if "security" in risk_flags:
        rec.append("Ограничьте хранение средств на биржах; используйте холодные кошельки/мульти-sig.")
    # если про war/geo
    if "geo" in risk_flags:
        rec.append("Держать часть капитала в защитных активах (золото, стабильные валюты).")

    # если ничего не сгенерилось — универсальная рекомендация
    if not rec:
        rec.append("Сохранять диверсификацию. Не паниковать — выбирать позиции по риск-менеджменту.")

    # краткий прогноз (упрощённо)
    forecast = "📈 Краткий прогноз: "
    # если BTC/Gold упоминаются — позитив для защитных активов
    combined_text = " ".join([orig + " " + ru for score, src, orig, ru in scored[:12]]).lower()
    if "bitcoin" in combined_text or "btc" in combined_text:
        forecast += "повышенная волатильность на крипто; возможны быстрые отскоки."
    elif "gold" in combined_text or "золото" in combined_text:
        forecast += "повышенный спрос на золото как защитный актив."
    else:
        forecast += "рынок может реагировать на новости — следите за топ-3 событиями."

    analysis_lines.append(forecast)
    analysis_lines.append("💡 Рекомендации:\n" + "\n".join(f"- {r}" for r in rec))

    analysis_text = "\n".join(analysis_lines)
    return top_insights, analysis_text

# ----------------- ОБНОВЛЕНИЕ CACHE в фоне -----------------
def update_loop():
    while True:
        try:
            CACHE["usd"] = fetch_usd_cbr()
            CACHE["btc"] = fetch_btc_binance()
            CACHE["gold"] = fetch_gold()
            CACHE["news"] = build_news_cache()
            CACHE["updated"] = datetime.utcnow().isoformat()
        except Exception:
            pass
        time.sleep(UPDATE_INTERVAL)

threading.Thread(target=update_loop, daemon=True).start()

# ----------------- TELEGRAM: меню и хэндлеры -----------------
def build_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(types.KeyboardButton("📊 Курсы"), types.KeyboardButton("🧠 Инсайды"))
    return kb

@bot.message_handler(commands=['start', 'help'])
def cmd_start(m):
    bot.send_message(m.chat.id,
                     "Привет 👋\nЯ CrypSideX — минималистичный бот для курсов и инсайдов.\n"
                     "Нажми кнопку ниже:", reply_markup=build_keyboard())

@bot.message_handler(func=lambda m: m.text == "📊 Курсы")
def cmd_rates(m):
    usd = CACHE.get("usd") or "—"
    btc = CACHE.get("btc") or "—"
    gold = CACHE.get("gold") or "—"
    ts = CACHE.get("updated") or "—"
    text = (f"📊 <b>Курсы (быстрая сводка)</b>\n\n"
            f"💵 USD (ЦБ) = <b>{usd}</b> ₽\n"
            f"₿ BTC = <b>{btc}</b> $\n"
            f"🥇 Gold (1 oz) = <b>{gold}</b> $\n\n"
            f"🕘 обновлено: {ts}")
    bot.send_message(m.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "🧠 Инсайды")
def cmd_insights(m):
    bot.send_chat_action(m.chat.id, "typing")
    news = CACHE.get("news", [])
    if not news:
        bot.send_message(m.chat.id, "Новости пока недоступны — попробуйте позже.")
        return
    # анализируем
    top_insights, analysis = analyze_headlines(news, top_n=5)
    # формируем сообщение: топ инсайдов + анализ
    msg = "<b>🧠 Топ инсайдов (собраны и переведены):</b>\n\n"
    for i, t in enumerate(top_insights, 1):
        msg += f"{i}. {t}\n"
    msg += "\n" + "<b>🤖 Краткая аналитика:</b>\n" + analysis
    bot.send_message(m.chat.id, msg)

@bot.message_handler(func=lambda m: True)
def fallback(m):
    bot.send_message(m.chat.id, "Нажми кнопку в меню 👇", reply_markup=build_keyboard())

# ----------------- СТАРТ -----------------
if __name__ == "__main__":
    print("CrypSideX v7 запущен. Жми /start в Telegram")
    # делаем начальное обновление синхронно, чтобы сразу были данные
    try:
        CACHE["usd"] = fetch_usd_cbr()
        CACHE["btc"] = fetch_btc_binance()
        CACHE["gold"] = fetch_gold()
        CACHE["news"] = build_news_cache()
        CACHE["updated"] = datetime.utcnow().isoformat()
    except Exception:
        pass
    bot.infinity_polling(timeout=10, long_polling_timeout=5)
0
