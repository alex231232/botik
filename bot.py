import telebot
from telebot import types
import time
import logging
import uuid
import threading
from flask import Flask, request, jsonify
import json
from config import config
from database import Database
from price_manager import PriceManager
from fragment_purchase import FragmentPurchase
from plategd_api import PlategdAPI
from image_manager import ImageManager
from message_manager import MessageManager
import utils
import os
import re
from functools import wraps
from threading import Thread
import traceback

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Инициализация
bot = telebot.TeleBot(config.BOT_TOKEN)
db = Database()
price_mgr = PriceManager(config.PRICE_FILE)
fragment = FragmentPurchase()
plategd = PlategdAPI()
image_mgr = ImageManager()
msg_manager = MessageManager(bot, delete_delay=3)


# === Глобальный обработчик ошибок ===
def error_handler(func):
    """Декоратор для перехвата ошибок и отправки админам"""

    @wraps(func)
    def wrapper(message, *args, **kwargs):
        try:
            return func(message, *args, **kwargs)
        except Exception as e:
            user_id = message.from_user.id
            username = message.from_user.username or message.from_user.first_name
            error_text = traceback.format_exc()

            # Отправляем пользователю вежливое сообщение
            try:
                bot.send_message(
                    user_id,
                    "❌ *Произошла ошибка*\n\n"
                    "Технические неполадки. Мы уже работаем над исправлением.\n"
                    "Пожалуйста, попробуйте позже.",
                    parse_mode='Markdown'
                )
            except:
                pass

            # Отправляем полную ошибку админам
            for admin_id in config.ADMIN_IDS:
                try:
                    error_message = f"""
⚠️ *ОШИБКА В БОТЕ*

👤 Пользователь: @{username} (ID: `{user_id}`)
📱 Команда: {message.text if hasattr(message, 'text') else 'Неизвестно'}

📋 *Трассировка ошибки:*
                    """
                    bot.send_message(admin_id, error_message, parse_mode='Markdown')
                except:
                    pass

            logger.error(f"Ошибка в обработчике для пользователя {user_id}: {e}")
            return None
    return wrapper


def callback_error_handler(func):
    """Декоратор для перехвата ошибок в callback'ах"""
    @wraps(func)
    def wrapper(call, *args, **kwargs):
        try:
            return func(call, *args, **kwargs)
        except Exception as e:
            user_id = call.from_user.id
            username = call.from_user.username or call.from_user.first_name
            error_text = traceback.format_exc()

            # Уведомляем пользователя
            try:
                bot.answer_callback_query(
                    call.id,
                    "❌ Произошла ошибка. Попробуйте позже.",
                    show_alert=True
                )
            except:
                pass

            # Отправляем ошибку админам
            for admin_id in config.ADMIN_IDS:
                try:
                    error_message = f"""
⚠️ *ОШИБКА В БОТЕ (CALLBACK)*

👤 Пользователь: @{username} (ID: `{user_id}`)
🔘 Callback data: {call.data}

📋 *Трассировка ошибки:*
                    """
                    bot.send_message(admin_id, error_message, parse_mode='Markdown')
                except:
                    pass

            logger.error(f"Ошибка в callback для пользователя {user_id}: {e}")
            return None

    return wrapper


# Декоратор для проверки бана
def check_banned(func):
    """Декоратор для проверки забанен ли пользователь"""

    @wraps(func)
    def wrapper(message):
        user_id = message.from_user.id

        # Проверяем, не забанен ли пользователь
        if db.is_user_banned(user_id):
            ban_info = db.get_ban_info(user_id)
            if ban_info:
                reason = ban_info.get('reason', 'Нарушение правил')
                banned_at = ban_info.get('banned_at', '')

                text = f"""
🚫 *Вы заблокированы в этом боте*

Причина: {reason}
Дата блокировки: {banned_at}

Если вы считаете это ошибкой, обратитесь в поддержку.
                """

                try:
                    bot.send_message(
                        user_id,
                        text,
                        parse_mode='Markdown'
                    )
                except:
                    pass
                return

        return func(message)

    return wrapper


# Декоратор для автоматического удаления предыдущих сообщений
def auto_cleanup(func):
    """Декоратор для автоматического удаления предыдущих сообщений"""

    @wraps(func)
    def wrapper(message):
        user_id = message.from_user.id
        chat_id = message.chat.id

        # Удаляем все предыдущие сообщения пользователя
        msg_manager.delete_previous_messages(user_id)

        # Добавляем текущее сообщение пользователя в историю
        msg_manager.add_message(user_id, chat_id, message.message_id)

        # Вызываем функцию
        result = func(message)

        return result

    return wrapper


# Создаем Flask приложение для вебхука
app = Flask(__name__)


# Декоратор для админов
def admin_required(func):
    @wraps(func)
    def wrapper(message):
        if utils.is_admin(message.from_user.id):
            return func(message)
        else:
            bot.reply_to(message, "❌ У вас нет прав для этой команды.")

    return wrapper


# === Flask вебхук для Platega.io ===
@app.route('/platega_webhook', methods=['POST', 'GET'])
def platega_webhook():
    """Эндпоинт для приема вебхуков от Platega.io"""
    if request.method == 'GET':
        logger.info("📩 Получен GET запрос на вебхук")
        return jsonify({
            "status": "ok",
            "message": "Webhook endpoint is working",
            "timestamp": time.time()
        }), 200

    try:
        data = request.json
        headers = dict(request.headers)

        logger.info("=" * 60)
        logger.info("📩 ПОЛУЧЕН ВЕБХУК ОТ PLATEGA.IO")
        logger.info(f"📩 Body: {json.dumps(data, indent=2, ensure_ascii=False)}")

        verified_data = plategd.verify_webhook(dict(headers), request.get_data())

        if verified_data:
            payment_id = verified_data.get('id') or verified_data.get('transactionId')
            status = verified_data.get('status', '').upper()

            logger.info(f"✅ Верифицирован платеж {payment_id} со статусом {status}")

            if status == 'CONFIRMED' or status == 'SUCCESS':
                metadata = {}
                if verified_data.get('payload'):
                    try:
                        metadata = json.loads(verified_data['payload'])
                        logger.info(f"📦 Метаданные из payload: {metadata}")
                    except:
                        metadata = {'raw': verified_data['payload']}

                user_id = metadata.get('user_id') or verified_data.get('user_id')
                stars_amount = metadata.get('stars_amount') or verified_data.get('stars_amount')
                recipient_username = metadata.get('recipient_username') or verified_data.get('recipient_username')

                logger.info(f"📦 user_id: {user_id}, stars_amount: {stars_amount}, recipient: {recipient_username}")

                if user_id and stars_amount:
                    try:
                        recipient = recipient_username if recipient_username else None

                        if recipient:
                            logger.info(f"💰 Отправка {stars_amount} ⭐ пользователю @{recipient}")
                        else:
                            user = bot.get_chat(int(user_id))
                            recipient = user.username
                            logger.info(f"💰 Отправка {stars_amount} ⭐ пользователю {user_id} (@{recipient})")

                        if recipient:
                            fragment_result = fragment.buy_stars(recipient, int(stars_amount))

                            if fragment_result['success']:
                                db.complete_payment(payment_id, fragment_result.get('transaction_id'))

                                try:
                                    bot.send_message(
                                        int(user_id),
                                        f"✅ *Платеж подтвержден!*\n\n"
                                        f"✨ {stars_amount} ⭐ успешно зачислены получателю @{recipient}!\n"
                                        f"Спасибо за покупку!",
                                        parse_mode='Markdown'
                                    )
                                    logger.info(f"✅ Уведомление отправлено пользователю {user_id}")
                                except Exception as e:
                                    logger.error(f"❌ Не удалось отправить уведомление пользователю {user_id}: {e}")

                                logger.info(f"✅ Успешно обработан вебхук для платежа {payment_id}")
                            else:
                                logger.error(f"❌ Ошибка отправки звезд: {fragment_result.get('error')}")

                                for admin_id in config.ADMIN_IDS:
                                    try:
                                        bot.send_message(
                                            admin_id,
                                            f"⚠️ СРОЧНО: Платеж {payment_id} прошел, но звезды не отправлены!\n"
                                            f"Пользователь: {user_id}\n"
                                            f"Получатель: @{recipient}\n"
                                            f"Сумма: {stars_amount} ⭐\n"
                                            f"Ошибка: {fragment_result.get('error')}"
                                        )
                                    except:
                                        pass
                        else:
                            logger.error(f"❌ Нет username для отправки")
                    except Exception as e:
                        logger.error(f"❌ Ошибка при обработке платежа: {e}")
                        import traceback
                        traceback.print_exc()
                else:
                    logger.warning(f"❌ Не найдены user_id или stars_amount в вебхуке")

            return jsonify({"status": "ok", "message": "Webhook processed"}), 200
        else:
            logger.warning("❌ Неверная подпись вебхука")
            return jsonify({"error": "Invalid signature"}), 401

    except Exception as e:
        logger.error(f"❌ Ошибка при обработке вебхука: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/health', methods=['GET'])
def health_check():
    """Эндпоинт для проверки здоровья сервера"""
    return jsonify({
        "status": "healthy",
        "timestamp": time.time(),
        "bot_working": True
    }), 200


@app.route('/', methods=['GET'])
def home():
    """Главная страница"""
    return jsonify({
        "name": "Platega Webhook Server",
        "status": "running",
        "endpoints": {
            "/": "GET - this info",
            "/health": "GET - health check",
            "/platega_webhook": "POST - webhook for Platega.io"
        }
    }), 200


def run_flask():
    """Запуск Flask приложения в отдельном потоке"""
    logger.info("🚀 Запуск Flask сервера для вебхуков...")
    try:
        app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
    except Exception as e:
        logger.error(f"❌ Ошибка запуска Flask: {e}")


# === Главное меню ===
def main_menu(user_id: int):
    """Создание главного меню"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)

    buttons = [
        types.KeyboardButton('💰 Купить звёзды'),
        types.KeyboardButton('📊 Моя статистика'),
    ]

    if utils.is_admin(user_id):
        buttons.append(types.KeyboardButton('⚙️ Админ панель'))

    markup.add(*buttons)
    return markup


# === Обработчики команд ===
@bot.message_handler(commands=['start'])
@auto_cleanup
@check_banned
@error_handler
def start_command(message):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    last_name = message.from_user.last_name

    db.add_user(user_id, username, first_name, last_name)

    current_price = price_mgr.get_price_per_star()

    welcome_text = f"""
🌟 Добро пожаловать, {first_name or 'пользователь'}!

💰 Покупка звёзд за рубли
Курс: ⭐️ 1 = {current_price} ₽

💡 Как это работает:
1. Вы выбираете количество звёзд
2. Указываете получателя (себя или друга)
3. Оплачиваете через Platega
4. После оплаты звёзды зачислятся получателю

📦 Доступные пакеты 👇
    """

    image_path = image_mgr.get_image_path('start')

    if image_path and os.path.exists(image_path):
        with open(image_path, 'rb') as start_photo:
            msg = bot.send_photo(
                message.chat.id,
                photo=start_photo,
                caption=welcome_text,
                reply_markup=main_menu(user_id)
            )
    else:
        msg = bot.send_message(
            message.chat.id,
            welcome_text,
            reply_markup=main_menu(user_id)
        )

    msg_manager.add_message(user_id, message.chat.id, msg.message_id)
    logger.info(f"Пользователь {user_id} запустил бота")


@bot.message_handler(commands=['info'])
@auto_cleanup
@check_banned
@error_handler
def info_command(message):
    """Обработчик команды /info"""
    user_id = message.from_user.id

    text = f"""
ℹ️ *Информация*

<blockquote>Я бот по продаже Stars 🌟</blockquote>

🧑‍💻 <a href="https://telegra.ph/Polzovatelskoe-soglashenie-04-01-19">Пользовательское соглашение</a>

<blockquote>🫂 Владелец: @zik1w</blockquote>

🛡 <a href="https://telegra.ph/Politika-konfidencialnosti-04-01-26">Политика конфиденциальности</a>

<blockquote>
🗣 Чат: @ZiKDonatChat
📢 Канал: @ZiK_Donat
📰 News: @ZiK_DonatNFT
</blockquote>

❤️ *Спасибо за то что выбрали нас!*
    """

    image_path = image_mgr.get_image_path('info')
    if image_path and os.path.exists(image_path):
        with open(image_path, 'rb') as photo:
            msg = bot.send_photo(
                user_id,
                photo=photo,
                caption=text,
                reply_markup=main_menu(user_id),
                parse_mode='HTML'
            )
    else:
        msg = bot.send_message(
            user_id,
            text,
            reply_markup=main_menu(user_id),
            parse_mode='HTML'
        )

    msg_manager.add_message(user_id, message.chat.id, msg.message_id)


@bot.message_handler(commands=['clear'])
@auto_cleanup
@check_banned
@error_handler
def clear_command(message):
    """Очистка истории сообщений"""
    user_id = message.from_user.id
    chat_id = message.chat.id

    msg_manager.clear_all(user_id)

    msg = bot.send_message(
        chat_id,
        "🧹 История сообщений очищена!",
        reply_markup=main_menu(user_id)
    )
    msg_manager.add_message(user_id, chat_id, msg.message_id)


@bot.message_handler(func=lambda message: message.text == '◀️ Вернуться в главное меню')
@auto_cleanup
@check_banned
@error_handler
def back_to_main_from_privacy(message):
    """Возврат в главное меню"""
    user_id = message.from_user.id
    msg = bot.send_message(
        user_id,
        "Главное меню:",
        reply_markup=main_menu(user_id)
    )
    msg_manager.add_message(user_id, message.chat.id, msg.message_id)


# === Покупка звезд ===
@bot.message_handler(func=lambda message: message.text == '💰 Купить звёзды')
@auto_cleanup
@check_banned
@error_handler
def buy_stars_menu(message):
    """Меню покупки звезд с выбором получателя"""
    user_id = message.from_user.id
    chat_id = message.chat.id

    if not message.from_user.username:
        msg = bot.send_message(
            user_id,
            "❌ Для получения звезд необходимо установить username в Telegram!\n\n"
            "Как установить: Настройки → Имя пользователя"
        )
        msg_manager.add_message(user_id, chat_id, msg.message_id)
        return

    packages = price_mgr.get_packages()
    current_price = price_mgr.get_price_per_star()
    balance = db.get_user_balance(user_id)

    # Создаем клавиатуру
    markup = types.InlineKeyboardMarkup(row_width=2)

    # Кнопки с пакетами
    buttons = []
    for amount in packages:
        rub_price = price_mgr.calculate_price(amount)
        button_text = f"{amount} ⭐ — {rub_price:.0f} ₽"
        buttons.append(
            types.InlineKeyboardButton(
                button_text,
                callback_data=f"select_recipient_{amount}"
            )
        )

    markup.add(*buttons)

    markup.add(
        types.InlineKeyboardButton("📦 Больше 2000 ⭐", callback_data="show_big_packages")
    )
    markup.add(
        types.InlineKeyboardButton("✏️ Другое количество", callback_data="buy_custom")
    )
    markup.add(
        types.InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")
    )

    balance_text = f"\n💰 Ваш баланс: {balance} ⭐" if balance > 0 else ""

    text = f"""
💰 Покупка звёзд за рубли

💰 Текущая цена: {current_price} ₽ за 1 ⭐{balance_text}

💡 Как это работает:
1. Вы выбираете количество звёзд
2. Указываете получателя (себя или друга)
3. Оплачиваете
4. После оплаты звёзды придут получателю

🌟 Для покупки более 2000 звезд нажмите "Больше 2000 ⭐"
    """

    msg_manager.prepare_for_new_message(user_id, chat_id)

    image_path = image_mgr.get_image_path('buy')
    if image_path and os.path.exists(image_path):
        with open(image_path, 'rb') as photo:
            msg = bot.send_photo(
                user_id,
                photo=photo,
                caption=text,
                reply_markup=markup
            )
    else:
        msg = bot.send_message(user_id, text, reply_markup=markup)

    msg_manager.add_message(user_id, chat_id, msg.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("select_recipient_"))
@callback_error_handler
def select_recipient(call):
    """Выбор получателя звезд"""
    stars_amount = int(call.data.replace("select_recipient_", ""))
    user_id = call.from_user.id
    chat_id = call.message.chat.id

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("👤 Себе", callback_data=f"recipient_self_{stars_amount}"),
        types.InlineKeyboardButton("👥 Другому пользователю", callback_data=f"recipient_other_{stars_amount}"),
        types.InlineKeyboardButton("◀️ Назад", callback_data="back_to_buy")
    )

    text = f"""
🎁 *Выберите получателя*

Количество: {stars_amount} ⭐

Кому отправить звёзды?
    """

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(
        chat_id,
        text,
        reply_markup=markup,
        parse_mode='Markdown'
    )
    msg_manager.add_message(user_id, chat_id, msg.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("recipient_self_"))
@callback_error_handler
def recipient_self(call):
    """Отправка себе"""
    stars_amount = int(call.data.replace("recipient_self_", ""))
    user_id = call.from_user.id
    chat_id = call.message.chat.id

    confirm_purchase(call, stars_amount, recipient_username=None, recipient_is_self=True)


@bot.callback_query_handler(func=lambda call: call.data.startswith("recipient_other_"))
@callback_error_handler
def recipient_other(call):
    """Запрос username получателя"""
    stars_amount = int(call.data.replace("recipient_other_", ""))
    user_id = call.from_user.id
    chat_id = call.message.chat.id

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(
        chat_id,
        f"✏️ *Отправка {stars_amount} ⭐ другому пользователю*\n\n"
        f"Введите username получателя (без @ или с @):\n"
        f"Пример: `username` или `@username`\n\n"
        f"Для отмены отправьте /cancel",
        parse_mode='Markdown'
    )
    msg_manager.add_message(user_id, chat_id, msg.message_id)
    bot.register_next_step_handler(msg, process_recipient_username, stars_amount)


def process_recipient_username(message, stars_amount):
    """Обработка username получателя"""
    user_id = message.from_user.id
    chat_id = message.chat.id

    msg_manager.add_message(user_id, chat_id, message.message_id)

    if message.text == '/cancel':
        msg = bot.send_message(
            chat_id,
            "❌ Отправка отменена",
            reply_markup=main_menu(user_id)
        )
        msg_manager.add_message(user_id, chat_id, msg.message_id)
        return

    username = message.text.strip().replace('@', '')

    if not username:
        msg = bot.send_message(
            chat_id,
            "❌ Введите корректный username"
        )
        msg_manager.add_message(user_id, chat_id, msg.message_id)
        return

    # Проверяем, существует ли пользователь
    try:
        recipient_user = bot.get_chat(f"@{username}")
        recipient_username = recipient_user.username

        if not recipient_username:
            msg = bot.send_message(
                chat_id,
                "❌ У этого пользователя нет username"
            )
            msg_manager.add_message(user_id, chat_id, msg.message_id)
            return

        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton("✅ Подтвердить",
                                       callback_data=f"confirm_recipient_{stars_amount}_{recipient_username}"),
            types.InlineKeyboardButton("◀️ Назад", callback_data=f"select_recipient_{stars_amount}")
        )

        text = f"""
👥 *Подтверждение получателя*

Получатель: @{recipient_username}
Количество: {stars_amount} ⭐

Всё верно?
        """

        msg_manager.prepare_for_new_message(user_id, chat_id)
        msg = bot.send_message(
            chat_id,
            text,
            reply_markup=markup,
            parse_mode='Markdown'
        )
        msg_manager.add_message(user_id, chat_id, msg.message_id)

    except Exception as e:
        msg = bot.send_message(
            chat_id,
            f"❌ Пользователь @{username} не найден в Telegram.\n"
            f"Проверьте правильность написания username."
        )
        msg_manager.add_message(user_id, chat_id, msg.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_recipient_"))
@callback_error_handler
def confirm_recipient(call):
    """Подтверждение получателя и переход к оплате"""
    parts = call.data.replace("confirm_recipient_", "").split('_')
    stars_amount = int(parts[0])
    recipient_username = parts[1]
    user_id = call.from_user.id
    chat_id = call.message.chat.id

    confirm_purchase(call, stars_amount, recipient_username, recipient_is_self=False)


def confirm_purchase(call, stars_amount, recipient_username=None, recipient_is_self=True):
    """Подтверждение покупки с выбором получателя"""
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    balance = db.get_user_balance(user_id)

    if recipient_is_self:
        recipient_display = f"@{call.from_user.username}"
        recipient_username = call.from_user.username
    else:
        recipient_display = f"@{recipient_username}"

    min_stars = price_mgr.get_min_stars()

    if stars_amount < min_stars:
        try:
            bot.answer_callback_query(
                call.id,
                f"❌ Минимальное количество для покупки: {min_stars} ⭐",
                show_alert=True
            )
        except:
            pass
        return

    price_rub = price_mgr.calculate_price(stars_amount)
    current_price = price_mgr.get_price_per_star()

    max_from_balance = stars_amount - min_stars
    from_balance = min(balance, max_from_balance)
    to_pay_stars = stars_amount - from_balance

    if to_pay_stars < min_stars:
        from_balance = stars_amount - min_stars
        to_pay_stars = min_stars

    to_pay_rub = price_mgr.calculate_price(to_pay_stars)

    markup = types.InlineKeyboardMarkup(row_width=2)

    if from_balance > 0:
        markup.add(
            types.InlineKeyboardButton("✅ Подтвердить",
                                       callback_data=f"confirm_buy_{stars_amount}_{to_pay_stars}_{from_balance}_{recipient_username}"),
            types.InlineKeyboardButton("◀️ Назад", callback_data="back_to_buy")
        )

        new_balance = balance - from_balance

        text = f"""
💰 *Подтверждение покупки*

👤 Получатель: {recipient_display}
✨ Всего звезд: {stars_amount} ⭐
💎 Сумма к оплате: {to_pay_rub:.2f} ₽ (за {to_pay_stars} ⭐)
💰 Списано с баланса: {from_balance} ⭐

📊 Баланс после покупки: {new_balance} ⭐

💡 Детализация:
• Обязательная оплата (минимум): {min_stars} ⭐
• Оплачено с баланса: {from_balance} ⭐
• Осталось на балансе: {new_balance} ⭐
        """
    else:
        markup.add(
            types.InlineKeyboardButton("✅ Подтвердить",
                                       callback_data=f"confirm_buy_{stars_amount}_{stars_amount}_0_{recipient_username}"),
            types.InlineKeyboardButton("◀️ Назад", callback_data="back_to_buy")
        )

        text = f"""
💰 *Подтверждение покупки*

👤 Получатель: {recipient_display}
✨ Количество: {stars_amount} ⭐
💎 Цена: {price_rub:.2f} ₽
💰 Курс: {current_price} ₽ за 1 ⭐
💰 Ваш баланс: {balance} ⭐ (недостаточно для списания)

Для подтверждения нажмите кнопку ниже:
        """

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(
        chat_id,
        text,
        reply_markup=markup,
        parse_mode='Markdown'
    )
    msg_manager.add_message(user_id, chat_id, msg.message_id)


@bot.message_handler(func=lambda message: message.text == '📊 Моя статистика')
@auto_cleanup
@check_banned
@error_handler
def show_stats(message):
    """Показ статистики пользователя (только завершённые покупки)"""
    user_id = message.from_user.id
    stats = db.get_user_stats(user_id)

    # Получаем ТОЛЬКО завершённые платежи
    completed_payments = db.get_user_completed_payments(user_id, 5)

    # Преобразуем в число
    try:
        total_rub = float(stats['total_rub'])
    except (ValueError, TypeError):
        total_rub = 0.0

    try:
        balance = int(stats['balance'])
    except (ValueError, TypeError):
        balance = 0

    try:
        total_stars = int(stats['total_stars'])
    except (ValueError, TypeError):
        total_stars = 0

    purchases_count = stats.get('purchases_count', 0)

    # Формируем текст покупок внутри ОДНОЙ цитаты
    purchases_lines = []
    if completed_payments:
        for payment in completed_payments:
            if len(payment) >= 4:
                stars, rub, status, date = payment[:4]
                date_str = date.strftime("%d.%m.%Y %H:%M") if hasattr(date, 'strftime') else str(date)
                rub_float = float(rub) if rub else 0.0
                purchases_lines.append(f"⭐️ {stars} — {rub_float:.2f} ₽ ({date_str})")
    else:
        purchases_lines.append("Пока нет завершённых покупок")

    # Объединяем все покупки в ОДНУ цитату
    if purchases_lines:
        purchases_quote = "<blockquote>\n" + "\n".join(purchases_lines) + "\n</blockquote>"
    else:
        purchases_quote = "<blockquote>Пока нет завершённых покупок</blockquote>"

    text = f"""
<b>📊 Ваш Профиль</b>

<blockquote>💰 Бонусный баланс: {balance} ⭐️</blockquote>

✨ Всего куплено: {total_stars} ⭐️

<blockquote>💵 Потрачено: {total_rub:.2f} ₽</blockquote>

<b>🛒 Последние завершённые покупки</b> 👇

{purchases_quote}

📦 Всего завершённых покупок: {purchases_count}
    """

    image_path = image_mgr.get_image_path('stats')
    if image_path and os.path.exists(image_path):
        with open(image_path, 'rb') as photo:
            msg = bot.send_photo(
                user_id,
                photo=photo,
                caption=text,
                reply_markup=main_menu(user_id),
                parse_mode='HTML'
            )
    else:
        msg = bot.send_message(
            user_id,
            text,
            reply_markup=main_menu(user_id),
            parse_mode='HTML'
        )

    msg_manager.add_message(user_id, message.chat.id, msg.message_id)


@bot.message_handler(commands=['test_buy'])
@admin_required
@auto_cleanup
@error_handler
def admin_test_buy_command(message):
    """Тестовая покупка звезд"""
    user_id = message.from_user.id
    chat_id = message.chat.id

    if not message.from_user.username:
        msg = bot.send_message(chat_id, "❌ У вас нет username!")
        msg_manager.add_message(user_id, chat_id, msg.message_id)
        return

    stars_amount = 50
    username = message.from_user.username

    msg = bot.send_message(chat_id, f"⏳ Тестовая покупка {stars_amount} ⭐ для @{username}...")
    msg_manager.add_message(user_id, chat_id, msg.message_id)

    result = fragment.buy_stars(username, stars_amount)

    if result['success']:
        text = f"""
✅ ТЕСТ УСПЕШЕН!

✨ {stars_amount} ⭐ отправлены @{username}
🆔 Транзакция: {result.get('transaction_id')}

💡 Если звезды не пришли, проверьте:
• Правильность API ключа
• Баланс на счету fragapi.ru
• Настройки API
        """
    else:
        text = f"""
❌ ТЕСТ НЕ УДАЛСЯ

Ошибка: {result.get('error')}

💡 Рекомендации:
{result.get('suggestion')}

📝 Проверьте:
1. API ключ в config.py
2. Баланс на fragapi.ru
3. Логи для деталей
        """

    msg = bot.send_message(chat_id, text)
    msg_manager.add_message(user_id, chat_id, msg.message_id)


# === Админ панель ===
@bot.message_handler(func=lambda message: message.text == '⚙️ Админ панель')
@admin_required
@auto_cleanup
@error_handler
def admin_panel(message):
    """Админ панель"""
    user_id = message.from_user.id
    current_price = price_mgr.get_price_per_star()

    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        types.InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"),
        types.InlineKeyboardButton("📦 Пакеты", callback_data="admin_packages"),
        types.InlineKeyboardButton("💵 Изменить цену", callback_data="admin_change_price"),
        types.InlineKeyboardButton("📈 Текущая цена", callback_data="admin_show_price"),
        types.InlineKeyboardButton("➕ Начислить баланс", callback_data="admin_add_balance"),
        types.InlineKeyboardButton("📊 Балансы пользователей", callback_data="admin_all_balances"),
        types.InlineKeyboardButton("🚫 Управление блокировками", callback_data="admin_ban_menu"),
        types.InlineKeyboardButton("🖼️ Управление изображениями", callback_data="admin_images_menu"),
        types.InlineKeyboardButton("◀️ Назад", callback_data="back_to_main")
    ]
    markup.add(*buttons)

    msg = bot.send_message(
        message.chat.id,
        f"⚙️ Админ панель\n\n"
        f"💰 Текущая цена: {current_price} ₽ за 1 ⭐\n"
        f"Выберите действие:",
        reply_markup=markup
    )

    msg_manager.add_message(user_id, message.chat.id, msg.message_id)


# === Callback обработчик ===
@bot.callback_query_handler(func=lambda call: True)
@callback_error_handler
def callback_handler(call):
    """Обработка всех callback запросов"""
    user_id = call.from_user.id
    chat_id = call.message.chat.id

    try:
        # Удаляем сообщение с кнопками
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception as e:
            logger.error(f"Ошибка при удалении сообщения с кнопками: {e}")

        # === НАВИГАЦИЯ ===
        if call.data == "back_to_main":
            msg_manager.clear_all(user_id)
            msg = bot.send_message(
                user_id,
                "Главное меню:",
                reply_markup=main_menu(user_id)
            )
            msg_manager.add_message(user_id, chat_id, msg.message_id)
            return

        elif call.data == "back_to_buy":
            msg_manager.clear_all(user_id)

            class MockMessage:
                def __init__(self, chat_id, from_user):
                    self.chat = type('obj', (object,), {'id': chat_id})
                    self.from_user = from_user
                    self.text = '💰 Купить звёзды'
                    self.message_id = 0
                    self.json = {}

            mock_message = MockMessage(chat_id, call.from_user)
            buy_stars_menu(mock_message)
            return

        # === ПОКУПКА ЗВЕЗД ===
        elif call.data == "buy_custom":
            ask_custom_amount(call)
            return

        elif call.data == "show_big_packages":
            msg_manager.clear_all(user_id)
            show_big_packages(call)
            return

        elif call.data.startswith("big_package_"):
            try:
                amount = int(call.data.replace("big_package_", ""))
                msg_manager.clear_all(user_id)
                big_package_selected(call, amount)
            except ValueError:
                logger.error(f"Неверный формат big_package: {call.data}")
            return

        elif call.data.startswith("confirm_buy_"):
            try:
                parts = call.data.replace("confirm_buy_", "").split('_')
                if len(parts) == 4:
                    stars_amount = int(parts[0])
                    to_pay_stars = int(parts[1])
                    from_balance = int(parts[2])
                    recipient_username = parts[3]
                    process_buy_stars(call, stars_amount, to_pay_stars, from_balance, recipient_username)
                else:
                    stars = int(call.data.replace("confirm_buy_", ""))
                    process_buy_stars(call, stars, stars, 0, None)
            except ValueError:
                logger.error(f"Неверный формат confirm_buy: {call.data}")
            return

        elif call.data.startswith("check_payment_"):
            payment_id = call.data.replace("check_payment_", "")
            check_payment_status(call, payment_id)
            return

        # === АДМИН ПАНЕЛЬ ===
        elif call.data.startswith("admin_"):
            if not utils.is_admin(user_id):
                try:
                    bot.answer_callback_query(call.id, "❌ Нет прав доступа", show_alert=True)
                except:
                    pass
                return

            msg_manager.clear_all(user_id)

            if call.data == "admin_stats":
                show_admin_stats(call)
            elif call.data == "admin_packages":
                show_admin_packages(call)
            elif call.data == "admin_change_price":
                ask_new_price(call)
            elif call.data == "admin_show_price":
                show_current_price(call)
            elif call.data == "admin_add_balance":
                ask_add_balance(call)
            elif call.data == "admin_all_balances":
                show_all_balances(call)
            elif call.data == "admin_ban_menu":
                admin_ban_menu(call)
            elif call.data == "admin_ban_user":
                ask_ban_username(call)
            elif call.data == "admin_unban_user":
                ask_unban_username(call)
            elif call.data == "admin_list_banned":
                show_banned_users(call)
            elif call.data == "admin_ban_info":
                ask_ban_info_username(call)
            elif call.data == "admin_images_menu":
                admin_images_menu(call)
            elif call.data == "admin_change_image_start":
                ask_new_image(call, 'start')
            elif call.data == "admin_change_image_buy":
                ask_new_image(call, 'buy')
            elif call.data == "admin_change_image_info":
                ask_new_image(call, 'info')
            elif call.data == "admin_change_image_stats":
                ask_new_image(call, 'stats')
            elif call.data == "admin_reset_images":
                reset_all_images(call)
            elif call.data == "admin_confirm_reset":
                confirm_reset_images(call)
            elif call.data == "admin_back":
                admin_back_to_panel(call)
            return

        # === ПОДТВЕРЖДЕНИЕ БАНА ===
        elif call.data.startswith("confirm_ban_"):
            parts = call.data.replace("confirm_ban_", "").split('|')
            if len(parts) == 2:
                user_id_to_ban = int(parts[0])
                reason = parts[1].replace('_', ' ')
                confirm_ban_user(call, user_id_to_ban, reason)
            return

        elif call.data.startswith("cancel_ban_"):
            user_id_to_ban = int(call.data.replace("cancel_ban_", ""))
            msg_manager.clear_all(user_id)
            msg = bot.send_message(
                chat_id,
                "❌ Блокировка отменена",
                reply_markup=main_menu(user_id)
            )
            msg_manager.add_message(user_id, chat_id, msg.message_id)
            return

        elif call.data.startswith("confirm_unban_"):
            user_id_to_unban = int(call.data.replace("confirm_unban_", ""))
            confirm_unban_user(call, user_id_to_unban)
            return

        elif call.data.startswith("force_add_balance_"):
            try:
                target_user_id = int(call.data.replace("force_add_balance_", ""))
                msg_manager.clear_all(user_id)
                msg = bot.send_message(
                    chat_id,
                    f"💰 Принудительное начисление баланса\n\n"
                    f"ID пользователя: `{target_user_id}`\n\n"
                    f"Введите количество звезд для начисления:",
                    parse_mode='Markdown'
                )
                msg_manager.add_message(user_id, chat_id, msg.message_id)
                bot.register_next_step_handler(msg, lambda m: process_add_balance_amount(m, target_user_id))
            except Exception as e:
                logger.error(f"Ошибка в force_add_balance_callback: {e}")
            return

        elif call.data == "admin_cancel":
            msg_manager.clear_all(user_id)
            msg = bot.send_message(
                chat_id,
                "❌ Действие отменено",
                reply_markup=main_menu(user_id)
            )
            msg_manager.add_message(user_id, chat_id, msg.message_id)
            return

        logger.warning(f"Неизвестный callback: {call.data}")

    except Exception as e:
        logger.error(f"Ошибка в callback: {e}")
        import traceback
        traceback.print_exc()


# === Функции для покупки звезд ===
def show_big_packages(call):
    """Показ больших пакетов (>2000 звезд)"""
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    first_name = call.from_user.first_name or 'пользователь'

    big_packages = price_mgr.get_big_packages()
    support_contact = price_mgr.get_support_contact()
    support_channel = price_mgr.get_support_channel()

    markup = types.InlineKeyboardMarkup(row_width=2)

    buttons = []
    for amount in big_packages:
        rub_price = price_mgr.calculate_price(amount)
        button_text = f"{amount} ⭐ — {rub_price:.0f} ₽"
        buttons.append(
            types.InlineKeyboardButton(
                button_text,
                callback_data=f"big_package_{amount}"
            )
        )

    markup.add(*buttons)
    markup.add(
        types.InlineKeyboardButton("◀️ Назад к покупке", callback_data="back_to_buy")
    )
    markup.add(
        types.InlineKeyboardButton("◀️ В главное меню", callback_data="back_to_main")
    )

    text = f"""
🌟 Крупные покупки звезд (>2000 ⭐)

👤 {first_name}, вы решились реально закупиться!

💬 Для покупки пакетов больше 2000 звезд, пожалуйста, свяжитесь с поддержкой:
🫂 Поддержка: {support_contact}
🗞️ Канал: {support_channel}

📝 Напишите в личные сообщения, и мы поможем с оформлением крупной покупки!
    """

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(chat_id, text, reply_markup=markup)
    msg_manager.add_message(user_id, chat_id, msg.message_id)


def big_package_selected(call, amount):
    """Обработка выбора большого пакета"""
    try:
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        first_name = call.from_user.first_name or 'пользователь'

        support_contact = price_mgr.get_support_contact()
        price = price_mgr.calculate_price(amount)

        text = f"""
🌟 {first_name}, вы решились реально закупиться!

Вы выбрали пакет: {amount} ⭐ на сумму {price:.2f} ₽

📦 Для покупки пакетов больше 2000 звезд, пожалуйста, свяжитесь с поддержкой:
🫂 Поддержка: {support_contact}
        """

        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("◀️ Назад к крупным пакетам", callback_data="show_big_packages"),
            types.InlineKeyboardButton("◀️ В главное меню", callback_data="back_to_main")
        )

        msg_manager.prepare_for_new_message(user_id, chat_id)
        msg = bot.send_message(chat_id, text, reply_markup=markup)
        msg_manager.add_message(user_id, chat_id, msg.message_id)

    except Exception as e:
        logger.error(f"Ошибка при выборе большого пакета: {e}")


def process_buy_stars(call, stars_amount, to_pay_stars, from_balance, recipient_username):
    """Обработка покупки звезд с учетом получателя"""
    user_id = call.from_user.id
    chat_id = call.message.chat.id

    if not recipient_username:
        recipient_username = call.from_user.username

    price_rub = price_mgr.calculate_price(to_pay_stars)

    try:
        user = bot.get_chat(user_id)
        username = user.username

        if not username:
            msg = bot.send_message(
                chat_id,
                "❌ У вас не установлен username в Telegram!"
            )
            msg_manager.add_message(user_id, chat_id, msg.message_id)
            return

        if from_balance > 0:
            if not db.deduct_from_balance(user_id, from_balance):
                msg = bot.send_message(
                    chat_id,
                    "❌ Ошибка списания с баланса. Попробуйте позже."
                )
                msg_manager.add_message(user_id, chat_id, msg.message_id)
                return
            logger.info(f"✅ Списано {from_balance} ⭐ с баланса пользователя {user_id}")

        bot_username = bot.get_me().username

        description = f"Покупка {stars_amount} ⭐ для @{recipient_username} (оплата {to_pay_stars} ⭐)"
        payment = plategd.create_payment(
            amount=price_rub,
            description=description,
            user_id=user_id,
            stars_amount=stars_amount,
            bot_username=bot_username,
            recipient_username=recipient_username
        )

        if payment['success']:
            db.create_payment(
                user_id,
                stars_amount,
                price_rub,
                payment['payment_id'],
                payment['transaction_id']
            )

            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                types.InlineKeyboardButton("💳 Оплатить", url=payment['payment_url'])
            )
            markup.add(
                types.InlineKeyboardButton("✅ Проверить оплату",
                                           callback_data=f"check_payment_{payment['payment_id']}")
            )
            markup.add(
                types.InlineKeyboardButton("◀️ Отмена", callback_data="back_to_main")
            )

            new_balance = db.get_user_balance(user_id)

            text = f"""
💰 Оформление покупки

👤 Получатель: @{recipient_username}
✨ Всего звезд: {stars_amount} ⭐
💎 Сумма к оплате: {price_rub:.2f} ₽ (за {to_pay_stars} ⭐)
💰 Списано с баланса: {from_balance} ⭐
📊 Новый баланс: {new_balance} ⭐

Для оплаты нажмите кнопку ниже:
            """

            msg_manager.prepare_for_new_message(user_id, chat_id)
            msg = bot.send_message(chat_id, text, reply_markup=markup)
            msg_manager.add_message(user_id, chat_id, msg.message_id)
        else:
            if from_balance > 0:
                db.add_to_balance(user_id, from_balance)
                logger.info(f"↩️ Возвращено {from_balance} ⭐ на баланс пользователя {user_id}")

            msg = bot.send_message(
                chat_id,
                f"❌ Ошибка: {payment.get('error', 'Неизвестная ошибка')}"
            )
            msg_manager.add_message(user_id, chat_id, msg.message_id)

    except Exception as e:
        logger.error(f"Ошибка в process_buy_stars: {e}")
        if from_balance > 0:
            db.add_to_balance(user_id, from_balance)
        msg = bot.send_message(chat_id, "❌ Произошла ошибка")
        msg_manager.add_message(user_id, chat_id, msg.message_id)


def check_payment_status(call, payment_id):
    """Проверка статуса платежа"""
    user_id = call.from_user.id
    chat_id = call.message.chat.id

    try:
        payment_info = db.get_pending_payment(payment_id)

        if not payment_info:
            try:
                bot.answer_callback_query(call.id, "❌ Платеж не найден", show_alert=True)
            except:
                pass
            return

        stars_amount = payment_info['stars_amount']

        msg_manager.prepare_for_new_message(user_id, chat_id)
        msg = bot.send_message(chat_id, f"⏳ Проверка статуса платежа...")
        msg_manager.add_message(user_id, chat_id, msg.message_id)

        result = plategd.check_payment(payment_id)

        is_paid = False

        if result['success']:
            if result.get('test_mode', False):
                is_paid = True
            else:
                is_paid = result.get('paid', False)

        if is_paid:
            msg_manager.prepare_for_new_message(user_id, chat_id)
            msg = bot.send_message(chat_id, f"⏳ Отправка {stars_amount} ⭐...")
            msg_manager.add_message(user_id, chat_id, msg.message_id)

            try:
                user = bot.get_chat(user_id)
                username = user.username
            except:
                username = None

            if not username:
                msg = bot.send_message(
                    chat_id,
                    "❌ Ошибка: не удалось получить username. Обратитесь в поддержку."
                )
                msg_manager.add_message(user_id, chat_id, msg.message_id)
                return

            fragment_result = fragment.buy_stars(username, stars_amount)

            if fragment_result['success']:
                db.complete_payment(payment_id, fragment_result.get('transaction_id'))
                stats = db.get_user_stats(user_id)

                mode_text = "🔧 ТЕСТОВЫЙ РЕЖИМ\n\n" if fragment_result.get('test_mode', False) else ""

                text = f"""
{mode_text}✅ Покупка успешна!

✨ {stars_amount} ⭐ зачислены
💎 Сумма: {payment_info['rub_amount']:.2f} ₽
📊 Всего куплено: {stats['total_stars']} ⭐
💰 Текущий баланс: {stats['balance']} ⭐
{fragment_result.get('message', '')}
                """

                msg_manager.prepare_for_new_message(user_id, chat_id)
                msg = bot.send_message(chat_id, text)
                msg_manager.add_message(user_id, chat_id, msg.message_id)
            else:
                error_msg = fragment_result.get('error', 'Неизвестная ошибка')
                suggestion = fragment_result.get('suggestion', '')

                text = f"""
❌ Ошибка отправки звезд

{error_msg}

{suggestion}

⚠️ Администратор уведомлен. Мы скоро решим проблему.
                """

                msg_manager.prepare_for_new_message(user_id, chat_id)
                msg = bot.send_message(chat_id, text)
                msg_manager.add_message(user_id, chat_id, msg.message_id)

                for admin_id in config.ADMIN_IDS:
                    try:
                        bot.send_message(
                            admin_id,
                            f"⚠️ СРОЧНО: Платеж {payment_id} прошел, но звезды не отправлены!\n"
                            f"Пользователь: {user_id} (@{username})\n"
                            f"Сумма: {stars_amount} ⭐\n"
                            f"Ошибка: {error_msg}"
                        )
                    except:
                        pass
        else:
            try:
                bot.answer_callback_query(
                    call.id,
                    "❌ Платеж еще не оплачен. После оплаты нажмите 'Проверить оплату' снова.",
                    show_alert=True
                )
            except:
                pass

    except Exception as e:
        logger.error(f"Ошибка в check_payment_status: {e}")
        import traceback
        traceback.print_exc()


def ask_custom_amount(call):
    """Запрос произвольного количества звезд"""
    try:
        user_id = call.from_user.id
        chat_id = call.message.chat.id

        msg_manager.delete_previous_messages(user_id, call.message.message_id)

        msg = bot.send_message(
            chat_id,
            "✏️ *Введите количество звезд*\n\n"
            f"Минимальное количество: {price_mgr.get_min_stars()} ⭐\n"
            f"Максимальное количество: {price_mgr.get_max_big_stars()} ⭐\n\n"
            "Пример: `150`",
            parse_mode='Markdown'
        )

        msg_manager.add_message(user_id, chat_id, msg.message_id)
        bot.register_next_step_handler(msg, process_custom_amount)

    except Exception as e:
        logger.error(f"Ошибка в ask_custom_amount: {e}")


def process_custom_amount(message):
    """Обработка произвольного количества"""
    user_id = message.from_user.id
    chat_id = message.chat.id

    msg_manager.add_message(user_id, chat_id, message.message_id)

    try:
        if message.text.startswith('/'):
            return

        amount = int(message.text.strip())
        first_name = message.from_user.first_name or 'пользователь'
        balance = db.get_user_balance(user_id)

        min_stars = price_mgr.get_min_stars()
        max_stars = price_mgr.get_max_stars()
        max_big_stars = price_mgr.get_max_big_stars()
        support_contact = price_mgr.get_support_contact()

        if amount < min_stars:
            msg = bot.send_message(chat_id, f"❌ Минимальное количество: {min_stars} ⭐")
            msg_manager.add_message(user_id, chat_id, msg.message_id)
            buy_stars_menu(message)
            return

        if amount > max_stars:
            if amount > max_big_stars:
                msg = bot.send_message(chat_id, f"❌ Максимальное количество: {max_big_stars} ⭐")
                msg_manager.add_message(user_id, chat_id, msg.message_id)
                buy_stars_menu(message)
                return

            price = price_mgr.calculate_price(amount)
            text = f"""
🌟 {first_name}, вы решились реально закупиться!

Вы выбрали: {amount} ⭐ на сумму {price:.2f} ₽

📦 Для покупки такого количества звезд (>2000 ⭐), пожалуйста, свяжитесь с поддержкой:
🫂 Поддержка: {support_contact}

💬 Напишите в личные сообщения, и мы поможем с оформлением крупной покупки!
            """

            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("◀️ Вернуться к покупке", callback_data="back_to_buy"),
                types.InlineKeyboardButton("◀️ В главное меню", callback_data="back_to_main")
            )

            msg = bot.send_message(chat_id, text, reply_markup=markup)
            msg_manager.add_message(user_id, chat_id, msg.message_id)
            return

        # Переходим к выбору получателя
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("👤 Себе", callback_data=f"recipient_self_{amount}"),
            types.InlineKeyboardButton("👥 Другому пользователю", callback_data=f"recipient_other_{amount}"),
            types.InlineKeyboardButton("◀️ Назад", callback_data="back_to_buy")
        )

        text = f"""
🎁 *Выберите получателя*

Количество: {amount} ⭐

Кому отправить звёзды?
        """

        msg_manager.prepare_for_new_message(user_id, chat_id)
        msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode='Markdown')
        msg_manager.add_message(user_id, chat_id, msg.message_id)

    except ValueError:
        msg = bot.send_message(chat_id, "❌ Пожалуйста, введите корректное число.")
        msg_manager.add_message(user_id, chat_id, msg.message_id)
        buy_stars_menu(message)
    except Exception as e:
        logger.error(f"Ошибка в process_custom_amount: {e}")
        msg = bot.send_message(chat_id, "❌ Произошла ошибка. Попробуйте снова.")
        msg_manager.add_message(user_id, chat_id, msg.message_id)
        buy_stars_menu(message)


# === Админские функции ===
def show_admin_stats(call):
    """Показ статистики для админа"""
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    stats = db.get_admin_stats()

    text = f"""
📊 Общая статистика

👥 Всего пользователей: {stats['total_users']}
✨ Всего продано звезд: {stats['total_stars']} ⭐
💰 Общая выручка: {stats['total_revenue']:.2f} ₽
📦 Всего покупок: {stats['total_purchases']}
⏳ Ожидают оплаты: {stats['pending_payments']}

💎 Средние показатели:
• Средний чек: {stats['avg_purchase']:.2f} ₽
• В среднем звезд за покупку: {stats['avg_stars']:.1f} ⭐
    """

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(chat_id, text)
    msg_manager.add_message(user_id, chat_id, msg.message_id)


def show_admin_packages(call):
    """Показ текущих пакетов звезд"""
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    packages = price_mgr.get_packages()
    big_packages = price_mgr.get_big_packages()
    current_price = price_mgr.get_price_per_star()

    text = f"""
📦 Пакеты звезд

💰 Текущая цена: {current_price} ₽ за 1 ⭐

📦 Стандартные пакеты (до 2000 ⭐):
{price_mgr.format_packages_for_display()}

🌟 Крупные пакеты (>2000 ⭐):
{price_mgr.format_big_packages_for_display()}

💡 Файл конфигурации:
`{config.PRICE_FILE}`

Для изменения пакетов отредактируйте файл вручную.
    """

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(chat_id, text, parse_mode='Markdown')
    msg_manager.add_message(user_id, chat_id, msg.message_id)


def ask_new_price(call):
    """Запрос новой цены у админа"""
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    current_price = price_mgr.get_price_per_star()

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(
        chat_id,
        f"💰 *Изменение цены*\n\n"
        f"Текущая цена: {current_price} ₽ за 1 ⭐\n\n"
        f"Введите новую цену за одну звезду (в рублях):\n"
        f"Пример: `1.8` или `2.0`",
        parse_mode='Markdown'
    )
    msg_manager.add_message(user_id, chat_id, msg.message_id)
    bot.register_next_step_handler(msg, process_new_price)


def process_new_price(message):
    """Обработка новой цены"""
    user_id = message.from_user.id
    chat_id = message.chat.id

    msg_manager.add_message(user_id, chat_id, message.message_id)

    try:
        new_price = float(message.text.strip().replace(',', '.'))

        if new_price <= 0:
            msg = bot.send_message(chat_id, "❌ Цена должна быть положительной!")
            msg_manager.add_message(user_id, chat_id, msg.message_id)
            return

        if new_price > 10:
            msg = bot.send_message(chat_id, "❌ Цена слишком высокая! Максимум 10 ₽ за звезду.")
            msg_manager.add_message(user_id, chat_id, msg.message_id)
            return

        admin_id = message.from_user.id
        db.set_price_per_star(new_price, admin_id)

        text = f"✅ Цена успешно изменена!\nНовая цена: {new_price} ₽ за 1 ⭐\n\nТеперь все покупки будут рассчитываться по новой цене."
        msg = bot.send_message(chat_id, text)
        msg_manager.add_message(user_id, chat_id, msg.message_id)

        logger.info(f"Админ {admin_id} изменил цену на {new_price} руб/звезда")

    except ValueError:
        msg = bot.send_message(chat_id, "❌ Введите корректное число (например: 1.8 или 2.0)")
        msg_manager.add_message(user_id, chat_id, msg.message_id)
    except Exception as e:
        logger.error(f"Ошибка при изменении цены: {e}")
        msg = bot.send_message(chat_id, "❌ Произошла ошибка при изменении цены")
        msg_manager.add_message(user_id, chat_id, msg.message_id)


def show_current_price(call):
    """Показ текущей цены и информации"""
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    current_price = price_mgr.get_price_per_star()

    examples = []
    for amount in [50, 100, 250, 500, 1000, 2000]:
        price = amount * current_price
        examples.append(f"• {amount} ⭐ = {price:.2f} ₽")

    text = f"""
💰 Текущая цена

Цена за 1 звезду: {current_price} ₽

📊 Примеры расчета:
{chr(10).join(examples)}

💡 Как изменить цену:
Нажмите "💵 Изменить цену" в админ панели
    """

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(chat_id, text)
    msg_manager.add_message(user_id, chat_id, msg.message_id)


def ask_add_balance(call):
    """Запрос ID пользователя для начисления баланса"""
    user_id = call.from_user.id
    chat_id = call.message.chat.id

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(
        chat_id,
        "➕ *Начисление баланса*\n\n"
        "Введите ID пользователя (число):\n"
        "Пример: `123456789`",
        parse_mode='Markdown'
    )
    msg_manager.add_message(user_id, chat_id, msg.message_id)
    bot.register_next_step_handler(msg, process_add_balance_user)


def process_add_balance_user(message):
    """Обработка ввода пользователя для начисления баланса"""
    user_id = message.from_user.id
    chat_id = message.chat.id

    msg_manager.add_message(user_id, chat_id, message.message_id)

    try:
        text = message.text.strip()

        try:
            target_user_id = int(text)

            with db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT username, first_name FROM users WHERE user_id = ?
                ''', (target_user_id,))
                user_data = cursor.fetchone()

            if user_data:
                username, first_name = user_data
                username_display = f"@{username}" if username else "нет username"

                text_msg = f"""
🔍 Найден пользователь в базе данных:
ID: `{target_user_id}`
Username: {username_display}
Имя: {first_name or 'не указано'}

Текущий баланс: {db.get_user_balance(target_user_id)} ⭐

Теперь введите количество звезд для начисления:
                """

                msg = bot.send_message(chat_id, text_msg, parse_mode='Markdown')
                msg_manager.add_message(user_id, chat_id, msg.message_id)
                bot.register_next_step_handler(msg, lambda m: process_add_balance_amount(m, target_user_id))
                return

            try:
                user = bot.get_chat(target_user_id)
                username = user.username or "нет username"
                first_name = user.first_name or ""

                text_msg = f"""
🔍 Найден пользователь в Telegram:
ID: `{target_user_id}`
Username: @{username}
Имя: {first_name}

Примечание: Пользователь еще не запускал бота.
Баланс будет начислен, но уведомление может не прийти.

Теперь введите количество звезд для начисления:
                """

                msg = bot.send_message(chat_id, text_msg, parse_mode='Markdown')
                msg_manager.add_message(user_id, chat_id, msg.message_id)
                bot.register_next_step_handler(msg, lambda m: process_add_balance_amount(m, target_user_id))

            except Exception as e:
                logger.error(f"Ошибка получения пользователя {target_user_id} из Telegram: {e}")

                markup = types.InlineKeyboardMarkup()
                markup.add(
                    types.InlineKeyboardButton("✅ Да, начислить", callback_data=f"force_add_balance_{target_user_id}"),
                    types.InlineKeyboardButton("❌ Отмена", callback_data="admin_cancel")
                )

                text_msg = f"""
⚠️ Пользователь с ID {target_user_id} не найден

Это может быть потому что:
• Пользователь еще не запускал бота
• Неверный ID
• Пользователь заблокировал бота

Начислить баланс в любом случае?
                """

                msg = bot.reply_to(message, text_msg, reply_markup=markup, parse_mode='Markdown')
                msg_manager.add_message(user_id, chat_id, msg.message_id)

        except ValueError:
            msg = bot.reply_to(message, "❌ Введите корректный числовой ID")
            msg_manager.add_message(user_id, chat_id, msg.message_id)

    except Exception as e:
        logger.error(f"Ошибка в process_add_balance_user: {e}")
        msg = bot.reply_to(message, "❌ Произошла ошибка")
        msg_manager.add_message(user_id, chat_id, msg.message_id)


def process_add_balance_amount(message, target_user_id):
    """Обработка ввода количества звезд для начисления"""
    user_id = message.from_user.id
    chat_id = message.chat.id

    msg_manager.add_message(user_id, chat_id, message.message_id)

    try:
        amount = int(message.text.strip())

        if amount <= 0:
            msg = bot.reply_to(message, "❌ Количество должно быть положительным числом")
            msg_manager.add_message(user_id, chat_id, msg.message_id)
            return

        if amount > 1000000:
            msg = bot.reply_to(message, "❌ Слишком большое количество (максимум 1,000,000 ⭐)")
            msg_manager.add_message(user_id, chat_id, msg.message_id)
            return

        admin_id = message.from_user.id

        if db.add_to_balance(target_user_id, amount, admin_id):
            try:
                user = bot.get_chat(target_user_id)
                user_info = f"@{user.username}" if user.username else f"ID: {target_user_id}"

                try:
                    bot.send_message(
                        target_user_id,
                        f"🎁 *Вам начислен бонус!*\n\n"
                        f"➕ {amount} ⭐ добавлено на ваш баланс!\n"
                        f"💰 Текущий баланс: {db.get_user_balance(target_user_id)} ⭐\n\n"
                        f"Теперь вы можете использовать их для покупки!",
                        parse_mode='Markdown'
                    )
                    notification_status = "✅ Уведомление отправлено"
                except Exception as e:
                    logger.error(f"Не удалось уведомить пользователя {target_user_id}: {e}")
                    notification_status = "⚠️ Не удалось отправить уведомление (пользователь не запускал бота)"

            except:
                user_info = f"ID: {target_user_id}"
                notification_status = "⚠️ Пользователь не найден в Telegram, уведомление не отправлено"

            text = f"""
✅ Успешно начислено {amount} ⭐

Пользователю: {user_info}
Новый баланс: {db.get_user_balance(target_user_id)} ⭐
{notification_status}
            """

            msg = bot.reply_to(message, text, parse_mode='Markdown')
            msg_manager.add_message(user_id, chat_id, msg.message_id)
        else:
            msg = bot.reply_to(message, "❌ Не удалось начислить баланс. Ошибка базы данных.")
            msg_manager.add_message(user_id, chat_id, msg.message_id)

    except ValueError:
        msg = bot.reply_to(message, "❌ Введите корректное число")
        msg_manager.add_message(user_id, chat_id, msg.message_id)
    except Exception as e:
        logger.error(f"Ошибка в process_add_balance_amount: {e}")
        msg = bot.reply_to(message, "❌ Произошла ошибка")
        msg_manager.add_message(user_id, chat_id, msg.message_id)


def show_all_balances(call):
    """Показ балансов всех пользователей"""
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    users = db.get_all_users_balance()

    if not users:
        text = "📊 Балансы пользователей\n\nНет пользователей с положительным балансом"
        msg_manager.prepare_for_new_message(user_id, chat_id)
        msg = bot.send_message(chat_id, text)
        msg_manager.add_message(user_id, chat_id, msg.message_id)
        return

    text = "📊 Балансы пользователей\n\n"
    total_balance = 0

    for i, (uid, username, first_name, balance) in enumerate(users[:20], 1):
        name = username or first_name or str(uid)
        name_display = f"@{name}" if username else name
        text += f"{i}. {name_display} — {balance} ⭐\n"
        total_balance += balance

    if len(users) > 20:
        text += f"\n... и еще {len(users) - 20} пользователей"

    text += f"\n\n💰 Общая сумма на балансах: {total_balance} ⭐"

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(chat_id, text)
    msg_manager.add_message(user_id, chat_id, msg.message_id)


# === Функции управления блокировками ===
def admin_ban_menu(call):
    """Меню управления блокировками"""
    user_id = call.from_user.id
    chat_id = call.message.chat.id

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🚫 Заблокировать пользователя", callback_data="admin_ban_user"),
        types.InlineKeyboardButton("✅ Разблокировать пользователя", callback_data="admin_unban_user"),
        types.InlineKeyboardButton("📋 Список заблокированных", callback_data="admin_list_banned"),
        types.InlineKeyboardButton("ℹ️ Информация о блокировке", callback_data="admin_ban_info"),
        types.InlineKeyboardButton("◀️ Назад в админку", callback_data="admin_back")
    )

    banned_count = db.get_banned_users_count()

    text = f"""
🚫 Управление блокировками

👥 Всего заблокировано: {banned_count} пользователей

Выберите действие:
    """

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(chat_id, text, reply_markup=markup)
    msg_manager.add_message(user_id, chat_id, msg.message_id)


def ask_ban_username(call):
    """Запрос username для блокировки"""
    user_id = call.from_user.id
    chat_id = call.message.chat.id

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(
        chat_id,
        "🚫 *Блокировка пользователя*\n\n"
        "Введите username пользователя (с @ или без):\n"
        "Пример: `@username` или `username`\n\n"
        "Для отмены отправьте /cancel",
        parse_mode='Markdown'
    )
    msg_manager.add_message(user_id, chat_id, msg.message_id)
    bot.register_next_step_handler(msg, process_ban_username)


def process_ban_username(message):
    """Обработка username для блокировки"""
    user_id = message.from_user.id
    chat_id = message.chat.id

    msg_manager.add_message(user_id, chat_id, message.message_id)

    if message.text == '/cancel':
        msg = bot.send_message(chat_id, "❌ Отменено", reply_markup=main_menu(user_id))
        msg_manager.add_message(user_id, chat_id, msg.message_id)
        return

    username = message.text.strip().replace('@', '')

    user_info = db.get_user_by_username(username)

    if not user_info:
        msg = bot.send_message(
            chat_id,
            f"❌ Пользователь @{username} не найден в базе данных.\n"
            f"Возможно, он еще не запускал бота."
        )
        msg_manager.add_message(user_id, chat_id, msg.message_id)
        return

    target_user_id = user_info['user_id']

    if db.is_user_banned(target_user_id):
        ban_info = db.get_ban_info(target_user_id)
        msg = bot.send_message(
            chat_id,
            f"❌ Пользователь @{username} уже заблокирован!\n"
            f"Причина: {ban_info.get('reason', 'Не указана')}\n"
            f"Дата: {ban_info.get('banned_at', 'Неизвестно')}"
        )
        msg_manager.add_message(user_id, chat_id, msg.message_id)
        return

    msg = bot.send_message(
        chat_id,
        f"🚫 *Блокировка пользователя @{username}*\n\n"
        f"ID: `{target_user_id}`\n"
        f"Имя: {user_info.get('first_name', 'Не указано')}\n\n"
        f"Введите причину блокировки:\n"
        f"Для отмены отправьте /cancel",
        parse_mode='Markdown'
    )
    msg_manager.add_message(user_id, chat_id, msg.message_id)
    bot.register_next_step_handler(msg, lambda m: process_ban_reason(m, target_user_id, username))


def process_ban_reason(message, target_user_id, username):
    """Обработка причины бана"""
    user_id = message.from_user.id
    chat_id = message.chat.id

    msg_manager.add_message(user_id, chat_id, message.message_id)

    if message.text == '/cancel':
        msg = bot.send_message(chat_id, "❌ Отменено", reply_markup=main_menu(user_id))
        msg_manager.add_message(user_id, chat_id, msg.message_id)
        return

    reason = message.text.strip()

    if len(reason) < 3:
        msg = bot.send_message(chat_id, "❌ Причина должна быть не менее 3 символов")
        msg_manager.add_message(user_id, chat_id, msg.message_id)
        bot.register_next_step_handler(msg, lambda m: process_ban_reason(m, target_user_id, username))
        return

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Подтвердить",
                                   callback_data=f"confirm_ban_{target_user_id}|{reason.replace(' ', '_')}"),
        types.InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_ban_{target_user_id}")
    )

    text = f"""
🚫 *Подтверждение блокировки*

Пользователь: @{username}
ID: `{target_user_id}`
Причина: {reason}

Вы уверены?
    """

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode='Markdown')
    msg_manager.add_message(user_id, chat_id, msg.message_id)


def confirm_ban_user(call, target_user_id, reason):
    """Подтверждение и выполнение бана"""
    admin_id = call.from_user.id
    chat_id = call.message.chat.id

    try:
        if db.ban_user(target_user_id, reason, admin_id):
            user_info = db.get_user_by_id(target_user_id)

            try:
                ban_text = f"""
🚫 *Вы были заблокированы в боте*

Причина: {reason}
Дата: {time.strftime('%d.%m.%Y %H:%M')}

Если вы считаете это ошибкой, обратитесь в поддержку.
                """
                bot.send_message(target_user_id, ban_text, parse_mode='Markdown')
                notification_status = "✅ Уведомление отправлено"
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление пользователю {target_user_id}: {e}")
                notification_status = "⚠️ Не удалось отправить уведомление"

            username = user_info.get('username', 'Неизвестно')
            if username:
                username = f"@{username}"

            text = f"""
✅ *Пользователь заблокирован!*

Пользователь: {username}
ID: `{target_user_id}`
Причина: {reason}
{notification_status}
            """

            msg_manager.prepare_for_new_message(admin_id, chat_id)
            msg = bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=main_menu(admin_id))
            msg_manager.add_message(admin_id, chat_id, msg.message_id)

            logger.info(f"Админ {admin_id} заблокировал пользователя {target_user_id} по причине: {reason}")

        else:
            msg = bot.send_message(chat_id, "❌ Не удалось заблокировать пользователя", reply_markup=main_menu(admin_id))
            msg_manager.add_message(admin_id, chat_id, msg.message_id)

    except Exception as e:
        logger.error(f"Ошибка при блокировке пользователя: {e}")
        msg = bot.send_message(chat_id, "❌ Произошла ошибка при блокировке", reply_markup=main_menu(admin_id))
        msg_manager.add_message(admin_id, chat_id, msg.message_id)


def ask_unban_username(call):
    """Запрос username для разблокировки"""
    user_id = call.from_user.id
    chat_id = call.message.chat.id

    banned_users = db.get_banned_users_list()

    if not banned_users:
        msg = bot.send_message(chat_id, "📋 Нет заблокированных пользователей", reply_markup=main_menu(user_id))
        msg_manager.add_message(user_id, chat_id, msg.message_id)
        return

    text = "📋 *Заблокированные пользователи:*\n\n"
    for i, user in enumerate(banned_users[:10], 1):
        username = f"@{user['username']}" if user['username'] else "Нет username"
        text += f"{i}. {username} (ID: `{user['user_id']}`)\n"
        text += f"   Причина: {user['reason']}\n"

    if len(banned_users) > 10:
        text += f"\n... и еще {len(banned_users) - 10} пользователей"

    text += "\n\nВведите username для разблокировки (с @ или без):\nДля отмены отправьте /cancel"

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(chat_id, text, parse_mode='Markdown')
    msg_manager.add_message(user_id, chat_id, msg.message_id)
    bot.register_next_step_handler(msg, process_unban_username)


def process_unban_username(message):
    """Обработка username для разблокировки"""
    user_id = message.from_user.id
    chat_id = message.chat.id

    msg_manager.add_message(user_id, chat_id, message.message_id)

    if message.text == '/cancel':
        msg = bot.send_message(chat_id, "❌ Отменено", reply_markup=main_menu(user_id))
        msg_manager.add_message(user_id, chat_id, msg.message_id)
        return

    username = message.text.strip().replace('@', '')

    user_info = db.get_user_by_username(username)

    if not user_info:
        msg = bot.send_message(chat_id, f"❌ Пользователь @{username} не найден в базе данных")
        msg_manager.add_message(user_id, chat_id, msg.message_id)
        return

    target_user_id = user_info['user_id']

    if not db.is_user_banned(target_user_id):
        msg = bot.send_message(chat_id, f"❌ Пользователь @{username} не заблокирован")
        msg_manager.add_message(user_id, chat_id, msg.message_id)
        return

    ban_info = db.get_ban_info(target_user_id)

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Разблокировать", callback_data=f"confirm_unban_{target_user_id}"),
        types.InlineKeyboardButton("❌ Отмена", callback_data="admin_ban_menu")
    )

    text = f"""
🔓 *Подтверждение разблокировки*

Пользователь: @{username}
ID: `{target_user_id}`
Причина бана: {ban_info.get('reason', 'Не указана')}
Дата бана: {ban_info.get('banned_at', 'Неизвестно')}

Вы уверены?
    """

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode='Markdown')
    msg_manager.add_message(user_id, chat_id, msg.message_id)


def confirm_unban_user(call, target_user_id):
    """Подтверждение и выполнение разбана"""
    admin_id = call.from_user.id
    chat_id = call.message.chat.id

    try:
        if db.unban_user(target_user_id, admin_id):
            user_info = db.get_user_by_id(target_user_id)

            try:
                unban_text = f"""
✅ *Вы были разблокированы в боте*

Теперь вы снова можете пользоваться ботом!
                """
                bot.send_message(target_user_id, unban_text, parse_mode='Markdown')
                notification_status = "✅ Уведомление отправлено"
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление пользователю {target_user_id}: {e}")
                notification_status = "⚠️ Не удалось отправить уведомление"

            username = user_info.get('username', 'Неизвестно')
            if username:
                username = f"@{username}"

            text = f"""
✅ *Пользователь разблокирован!*

Пользователь: {username}
ID: `{target_user_id}`
{notification_status}
            """

            msg_manager.prepare_for_new_message(admin_id, chat_id)
            msg = bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=main_menu(admin_id))
            msg_manager.add_message(admin_id, chat_id, msg.message_id)

            logger.info(f"Админ {admin_id} разблокировал пользователя {target_user_id}")

        else:
            msg = bot.send_message(chat_id, "❌ Не удалось разблокировать пользователя",
                                   reply_markup=main_menu(admin_id))
            msg_manager.add_message(admin_id, chat_id, msg.message_id)

    except Exception as e:
        logger.error(f"Ошибка при разблокировке пользователя: {e}")
        msg = bot.send_message(chat_id, "❌ Произошла ошибка при разблокировке", reply_markup=main_menu(admin_id))
        msg_manager.add_message(admin_id, chat_id, msg.message_id)


def show_banned_users(call):
    """Показ списка заблокированных пользователей"""
    user_id = call.from_user.id
    chat_id = call.message.chat.id

    banned_users = db.get_banned_users_list()

    if not banned_users:
        text = "📋 Нет заблокированных пользователей"
    else:
        text = f"📋 *Заблокированные пользователи ({len(banned_users)}):*\n\n"
        for i, user in enumerate(banned_users, 1):
            username = f"@{user['username']}" if user['username'] else "Нет username"
            text += f"{i}. {username}\n"
            text += f"   ID: `{user['user_id']}`\n"
            text += f"   Причина: {user['reason']}\n"
            text += f"   Дата: {user['banned_at']}\n"
            if user['banned_by']:
                text += f"   Заблокировал: {user['banned_by']}\n"
            text += "\n"

            if i >= 20:
                text += f"\n... и еще {len(banned_users) - 20} пользователей"
                break

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(chat_id, text, parse_mode='Markdown')
    msg_manager.add_message(user_id, chat_id, msg.message_id)


def ask_ban_info_username(call):
    """Запрос username для получения информации о бане"""
    user_id = call.from_user.id
    chat_id = call.message.chat.id

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(
        chat_id,
        "ℹ️ *Информация о блокировке*\n\n"
        "Введите username пользователя (с @ или без):\n"
        "Для отмены отправьте /cancel",
        parse_mode='Markdown'
    )
    msg_manager.add_message(user_id, chat_id, msg.message_id)
    bot.register_next_step_handler(msg, process_ban_info_username)


def process_ban_info_username(message):
    """Обработка запроса информации о бане"""
    user_id = message.from_user.id
    chat_id = message.chat.id

    msg_manager.add_message(user_id, chat_id, message.message_id)

    if message.text == '/cancel':
        msg = bot.send_message(chat_id, "❌ Отменено", reply_markup=main_menu(user_id))
        msg_manager.add_message(user_id, chat_id, msg.message_id)
        return

    username = message.text.strip().replace('@', '')

    user_info = db.get_user_by_username(username)

    if not user_info:
        msg = bot.send_message(chat_id, f"❌ Пользователь @{username} не найден в базе данных")
        msg_manager.add_message(user_id, chat_id, msg.message_id)
        return

    target_user_id = user_info['user_id']

    if not db.is_user_banned(target_user_id):
        msg = bot.send_message(chat_id, f"✅ Пользователь @{username} не заблокирован")
        msg_manager.add_message(user_id, chat_id, msg.message_id)
        return

    ban_info = db.get_ban_info(target_user_id)

    text = f"""
ℹ️ *Информация о блокировке*

Пользователь: @{username}
ID: `{target_user_id}`
Имя: {user_info.get('first_name', 'Не указано')} {user_info.get('last_name', '')}

🚫 *Статус:* ЗАБЛОКИРОВАН
Причина: {ban_info.get('reason', 'Не указана')}
Дата блокировки: {ban_info.get('banned_at', 'Неизвестно')}
Заблокировал: {ban_info.get('banned_by', 'Неизвестно')}
    """

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(chat_id, text, parse_mode='Markdown')
    msg_manager.add_message(user_id, chat_id, msg.message_id)


# === Функции для управления изображениями ===
def admin_images_menu(call):
    """Меню управления изображениями"""
    user_id = call.from_user.id
    chat_id = call.message.chat.id

    images_status = image_mgr.get_images_list_for_display()

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("🖼️ Сменить стартовое фото", callback_data="admin_change_image_start"),
        types.InlineKeyboardButton("🖼️ Сменить фото покупки", callback_data="admin_change_image_buy"),
        types.InlineKeyboardButton("🖼️ Сменить фото информации", callback_data="admin_change_image_info"),
        types.InlineKeyboardButton("🖼️ Сменить фото статистики", callback_data="admin_change_image_stats"),
        types.InlineKeyboardButton("🗑️ Сбросить все фото", callback_data="admin_reset_images"),
        types.InlineKeyboardButton("◀️ Назад в админку", callback_data="admin_back")
    )

    text = f"""
🖼️ Управление изображениями

{images_status}

💡 Как сменить изображение:
1. Нажмите на кнопку нужного меню
2. Отправьте новое фото
3. Изображение сразу обновится

📁 Папка с изображениями: `{image_mgr.images_folder}`
    """

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(chat_id, text, reply_markup=markup, parse_mode='Markdown')
    msg_manager.add_message(user_id, chat_id, msg.message_id)


def ask_new_image(call, image_key: str):
    """Запрос нового изображения"""
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    image_name = image_mgr.IMAGE_NAMES.get(image_key, image_key)

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(
        chat_id,
        f"🖼️ *Смена изображения для:* {image_name}\n\n"
        f"Отправьте новое фото (JPEG или PNG).\n"
        f"Для отмены отправьте /cancel",
        parse_mode='Markdown'
    )
    msg_manager.add_message(user_id, chat_id, msg.message_id)
    bot.register_next_step_handler(msg, process_new_image, image_key)


def process_new_image(message, image_key: str):
    """Обработка нового изображения"""
    user_id = message.from_user.id
    chat_id = message.chat.id

    msg_manager.add_message(user_id, chat_id, message.message_id)

    if message.text and message.text == '/cancel':
        msg = bot.send_message(chat_id, "❌ Отменено", reply_markup=main_menu(user_id))
        msg_manager.add_message(user_id, chat_id, msg.message_id)
        return

    if not message.photo:
        msg = bot.send_message(chat_id, "❌ Пожалуйста, отправьте фото")
        msg_manager.add_message(user_id, chat_id, msg.message_id)

        new_msg = bot.send_message(
            chat_id,
            f"🖼️ *Смена изображения для:* {image_mgr.IMAGE_NAMES.get(image_key, image_key)}\n\n"
            f"Отправьте новое фото.\nДля отмены отправьте /cancel",
            parse_mode='Markdown'
        )
        msg_manager.add_message(user_id, chat_id, new_msg.message_id)
        bot.register_next_step_handler(new_msg, process_new_image, image_key)
        return

    try:
        file_id = message.photo[-1].file_id
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        admin_id = message.from_user.id

        if image_mgr.save_image(image_key, downloaded_file, admin_id):
            text = f"✅ Изображение для *{image_mgr.IMAGE_NAMES.get(image_key, image_key)}* успешно обновлено!"

            msg_manager.prepare_for_new_message(user_id, chat_id)
            reply_msg = bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=main_menu(user_id))
            msg_manager.add_message(user_id, chat_id, reply_msg.message_id)

            image_path = image_mgr.get_image_path(image_key)
            if image_path and os.path.exists(image_path):
                with open(image_path, 'rb') as photo:
                    photo_msg = bot.send_photo(chat_id, photo, caption="🖼️ *Новое изображение*", parse_mode='Markdown')
                    msg_manager.add_message(user_id, chat_id, photo_msg.message_id)
        else:
            msg = bot.send_message(chat_id, "❌ Ошибка при сохранении изображения", reply_markup=main_menu(user_id))
            msg_manager.add_message(user_id, chat_id, msg.message_id)

    except Exception as e:
        logger.error(f"Ошибка при сохранении изображения: {e}")
        msg = bot.send_message(chat_id, "❌ Произошла ошибка при сохранении", reply_markup=main_menu(user_id))
        msg_manager.add_message(user_id, chat_id, msg.message_id)


def reset_all_images(call):
    """Сброс всех изображений к дефолтным"""
    user_id = call.from_user.id
    chat_id = call.message.chat.id

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ Да, сбросить все", callback_data="admin_confirm_reset"),
        types.InlineKeyboardButton("❌ Нет, отмена", callback_data="admin_images_menu")
    )

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(
        chat_id,
        "⚠️ *Вы уверены?*\n\n"
        "Все кастомные изображения будут удалены.\n"
        "Бот вернется к использованию файлов по умолчанию.",
        reply_markup=markup,
        parse_mode='Markdown'
    )
    msg_manager.add_message(user_id, chat_id, msg.message_id)


def confirm_reset_images(call):
    """Подтверждение сброса всех изображений"""
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    admin_id = call.from_user.id
    reset_count = 0

    for key in image_mgr.DEFAULT_IMAGES:
        if image_mgr.delete_image(key, admin_id):
            reset_count += 1

    msg_manager.prepare_for_new_message(user_id, chat_id)
    msg = bot.send_message(
        chat_id,
        f"✅ *Сброс завершен*\n\n"
        f"Удалено кастомных изображений: {reset_count}\n"
        f"Теперь бот использует файлы по умолчанию.",
        parse_mode='Markdown'
    )
    msg_manager.add_message(user_id, chat_id, msg.message_id)


def admin_back_to_panel(call):
    """Возврат в админ панель"""
    try:
        class MockMessage:
            def __init__(self, chat_id, from_user):
                self.chat = type('obj', (object,), {'id': chat_id})
                self.from_user = from_user
                self.text = '⚙️ Админ панель'
                self.message_id = 0
                self.json = {}

        mock_message = MockMessage(call.message.chat.id, call.from_user)
        msg_manager.prepare_for_new_message(call.from_user.id, call.message.chat.id)
        admin_panel(mock_message)

    except Exception as e:
        logger.error(f"Ошибка в admin_back_to_panel: {e}")
        msg = bot.send_message(
            call.message.chat.id,
            "❌ Произошла ошибка при возврате в админ панель",
            reply_markup=main_menu(call.from_user.id)
        )
        msg_manager.add_message(call.from_user.id, call.message.chat.id, msg.message_id)


# === Запуск бота ===
def main():
    """Запуск бота"""
    logger.info("=" * 60)
    logger.info("🚀 Бот для покупки звезд за рубли запущен")

    if not all([config.PLATEGD_PROJECT_ID, config.PLATEGD_SECRET_KEY]):
        logger.warning("⚠️ Не все данные Platega.io заполнены!")
    else:
        logger.info(f"✅ Platega.io настроен с Project ID: {config.PLATEGD_PROJECT_ID[:8]}...")
        logger.info(f"📞 Callback URL: https://ywfox.site/platega_webhook")

    try:
        balance = fragment.check_balance()
        logger.info(f"💰 Баланс Fragment: {balance:.2f} TON")
    except Exception as e:
        logger.warning(f"⚠️ Не удалось проверить баланс Fragment: {e}")

    current_price = price_mgr.get_price_per_star()
    logger.info(f"💰 Текущая цена: {current_price} ₽ за 1 ⭐")
    logger.info(f"📦 Загружено {len(price_mgr.get_packages())} стандартных пакетов")
    logger.info(f"📦 Загружено {len(price_mgr.get_big_packages())} крупных пакетов")
    logger.info("=" * 60)

    try:
        flask_thread = Thread(target=run_flask, daemon=True)
        flask_thread.start()
        logger.info("🚀 Вебхук сервер запущен на порту 5000")
        logger.info("📞 URL вебхука: https://ywfox.site/platega_webhook")
        logger.info("🏥 Health check: https://ywfox.site/health")
    except Exception as e:
        logger.error(f"❌ Ошибка запуска вебхук сервера: {e}")
        logger.warning("⚠️ Продолжаем работу без вебхук сервера")

    logger.info("=" * 60)
    logger.info("🤖 Запуск Telegram бота...")

    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as e:
            logger.error(f"❌ Ошибка бота: {e}")
            logger.info("🔄 Перезапуск через 5 секунд...")
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("👋 Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"❌ Фатальная ошибка: {e}")
        import traceback

        traceback.print_exc()