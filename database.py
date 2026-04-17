import sqlite3
from datetime import datetime
from typing import Optional, List, Dict, Tuple
import logging

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_name='bot_database.db'):
        self.db_name = db_name
        self.init_db()

    def get_connection(self):
        return sqlite3.connect(self.db_name, detect_types=sqlite3.PARSE_DECLTYPES)

    def init_db(self):
        """Инициализация базы данных"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                # Таблица пользователей
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        username TEXT,
                        first_name TEXT,
                        last_name TEXT,
                        registered_at TIMESTAMP,
                        total_stars_purchased INTEGER DEFAULT 0,
                        total_rub_spent REAL DEFAULT 0,
                        balance INTEGER DEFAULT 0
                    )
                ''')

                # Таблица платежей
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS payments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        stars_amount INTEGER NOT NULL,
                        rub_amount REAL NOT NULL,
                        payment_id TEXT UNIQUE,
                        order_id TEXT UNIQUE,
                        status TEXT DEFAULT 'pending',
                        created_at TIMESTAMP,
                        completed_at TIMESTAMP,
                        fragment_tx_id TEXT,
                        FOREIGN KEY (user_id) REFERENCES users (user_id)
                    )
                ''')

                # Таблица настроек (для хранения цены)
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        updated_at TIMESTAMP,
                        updated_by INTEGER
                    )
                ''')

                # Таблица заблокированных пользователей
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS banned_users (
                        user_id INTEGER PRIMARY KEY,
                        reason TEXT,
                        banned_by INTEGER,
                        banned_at TIMESTAMP,
                        FOREIGN KEY (user_id) REFERENCES users(user_id)
                    )
                ''')

                # Таблица истории банов
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS ban_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER,
                        action TEXT,
                        performed_by INTEGER,
                        performed_at TIMESTAMP,
                        FOREIGN KEY (user_id) REFERENCES users(user_id)
                    )
                ''')

                # Индексы
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status)')

                # Добавляем начальную цену по умолчанию, если её нет
                cursor.execute('''
                    INSERT OR IGNORE INTO settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                ''', ('price_per_star', '1.6', datetime.now()))

                conn.commit()
                logger.info("База данных инициализирована")

        except Exception as e:
            logger.error(f"Ошибка инициализации БД: {e}")
            raise

    # === Методы для пользователей ===
    def add_user(self, user_id: int, username: str, first_name: str, last_name: str) -> bool:
        """Добавление нового пользователя"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR IGNORE INTO users 
                    (user_id, username, first_name, last_name, registered_at)
                    VALUES (?, ?, ?, ?, ?)
                ''', (user_id, username or '', first_name or '', last_name or '', datetime.now()))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка добавления пользователя {user_id}: {e}")
            return False

    def get_user_stats(self, user_id: int) -> Dict:
        """Получение статистики пользователя"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                # Получаем общую статистику пользователя
                cursor.execute('''
                    SELECT total_stars_purchased, total_rub_spent, balance
                    FROM users WHERE user_id = ?
                ''', (user_id,))
                result = cursor.fetchone()

                if result:
                    total_stars = result[0] or 0
                    total_rub = result[1] or 0
                    balance = result[2] or 0
                else:
                    total_stars = 0
                    total_rub = 0
                    balance = 0

                # Количество успешных покупок
                cursor.execute('''
                    SELECT COUNT(*) FROM payments 
                    WHERE user_id = ? AND status = 'completed'
                ''', (user_id,))
                purchases_count = cursor.fetchone()[0]

                return {
                    'total_stars': total_stars,
                    'total_rub': total_rub,
                    'balance': balance,
                    'purchases_count': purchases_count
                }
        except Exception as e:
            logger.error(f"Ошибка получения статистики пользователя {user_id}: {e}")
            return {
                'total_stars': 0,
                'total_rub': 0,
                'balance': 0,
                'purchases_count': 0
            }

    # === Методы для работы с балансом ===
    def get_user_balance(self, user_id: int) -> int:
        """Получение баланса пользователя"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
                result = cursor.fetchone()
                return result[0] if result else 0
        except Exception as e:
            logger.error(f"Ошибка получения баланса пользователя {user_id}: {e}")
            return 0

    def add_to_balance(self, user_id: int, stars_amount: int, admin_id: int = None) -> bool:
        """Добавление звезд на баланс пользователя"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                # Проверяем, существует ли пользователь
                cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
                user_exists = cursor.fetchone()

                if not user_exists:
                    # Если пользователя нет, создаем запись
                    cursor.execute('''
                        INSERT INTO users (user_id, username, first_name, last_name, registered_at, balance)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (user_id, '', '', '', datetime.now(), stars_amount))
                    logger.info(f"Создана новая запись для пользователя {user_id} с балансом {stars_amount} ⭐")
                else:
                    # Если пользователь есть, обновляем баланс
                    cursor.execute('''
                        UPDATE users 
                        SET balance = balance + ? 
                        WHERE user_id = ?
                    ''', (stars_amount, user_id))

                conn.commit()

                logger.info(f"Добавлено {stars_amount} ⭐ на баланс пользователя {user_id}" +
                            (f" админом {admin_id}" if admin_id else ""))
                return True
        except Exception as e:
            logger.error(f"Ошибка добавления на баланс: {e}")
            return False

    def deduct_from_balance(self, user_id: int, stars_amount: int) -> bool:
        """Списание звезд с баланса при покупке"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                # Проверяем текущий баланс
                current_balance = self.get_user_balance(user_id)
                if current_balance < stars_amount:
                    logger.warning(f"Недостаточно средств на балансе у {user_id}: {current_balance} < {stars_amount}")
                    return False

                # Списываем звезды
                cursor.execute('''
                    UPDATE users 
                    SET balance = balance - ? 
                    WHERE user_id = ? AND balance >= ?
                ''', (stars_amount, user_id, stars_amount))

                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Ошибка списания с баланса: {e}")
            return False

    def get_all_users_balance(self) -> List[Tuple]:
        """Получение баланса всех пользователей (для админа)"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT user_id, username, first_name, balance 
                    FROM users 
                    WHERE balance > 0 
                    ORDER BY balance DESC
                ''')
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Ошибка получения балансов пользователей: {e}")
            return []

    # === Методы для платежей ===
    def create_payment(self, user_id: int, stars_amount: int, rub_amount: float,
                       payment_id: str, order_id: str) -> Optional[int]:
        """Создание записи о платеже"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO payments 
                    (user_id, stars_amount, rub_amount, payment_id, order_id, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (user_id, stars_amount, rub_amount, payment_id, order_id, 'pending', datetime.now()))
                payment_record_id = cursor.lastrowid
                conn.commit()
                return payment_record_id
        except Exception as e:
            logger.error(f"Ошибка создания платежа: {e}")
            return None

    def complete_payment(self, payment_id: str, fragment_tx_id: str = None) -> bool:
        """Завершение платежа"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                # Получаем данные платежа
                cursor.execute('''
                    SELECT user_id, stars_amount, rub_amount 
                    FROM payments WHERE payment_id = ? AND status = 'pending'
                ''', (payment_id,))
                payment = cursor.fetchone()

                if not payment:
                    logger.warning(f"Платеж {payment_id} не найден или уже обработан")
                    return False

                user_id, stars_amount, rub_amount = payment

                # Обновляем статус платежа
                cursor.execute('''
                    UPDATE payments 
                    SET status = 'completed', completed_at = ?, fragment_tx_id = ?
                    WHERE payment_id = ?
                ''', (datetime.now(), fragment_tx_id, payment_id))

                # Обновляем статистику пользователя
                cursor.execute('''
                    UPDATE users 
                    SET total_stars_purchased = total_stars_purchased + ?,
                        total_rub_spent = total_rub_spent + ?
                    WHERE user_id = ?
                ''', (stars_amount, rub_amount, user_id))

                conn.commit()
                logger.info(f"Платеж {payment_id} успешно завершен")
                return True

        except Exception as e:
            logger.error(f"Ошибка завершения платежа {payment_id}: {e}")
            return False

    def get_pending_payment(self, payment_id: str) -> Optional[Dict]:
        """Получение информации о pending платеже"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT user_id, stars_amount, rub_amount 
                    FROM payments 
                    WHERE payment_id = ? AND status = 'pending'
                ''', (payment_id,))
                result = cursor.fetchone()
                if result:
                    return {
                        'user_id': result[0],
                        'stars_amount': result[1],
                        'rub_amount': result[2]
                    }
                return None
        except Exception as e:
            logger.error(f"Ошибка получения pending платежа {payment_id}: {e}")
            return None

    def get_user_payments(self, user_id: int, limit: int = 10) -> List[Tuple]:
        """Получение истории всех платежей пользователя (включая pending)"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT stars_amount, rub_amount, status, created_at, fragment_tx_id
                    FROM payments 
                    WHERE user_id = ? 
                    ORDER BY created_at DESC 
                    LIMIT ?
                ''', (user_id, limit))
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Ошибка получения платежей пользователя {user_id}: {e}")
            return []

    def get_user_completed_payments(self, user_id: int, limit: int = 5) -> List[Tuple]:
        """Получение только завершённых платежей пользователя"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT stars_amount, rub_amount, status, created_at, fragment_tx_id
                    FROM payments 
                    WHERE user_id = ? AND status = 'completed'
                    ORDER BY created_at DESC 
                    LIMIT ?
                ''', (user_id, limit))
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Ошибка получения завершённых платежей пользователя {user_id}: {e}")
            return []

    # === Методы для настроек (цена за звезду) ===
    def get_price_per_star(self) -> float:
        """Получение текущей цены за одну звезду"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT value FROM settings WHERE key = 'price_per_star'
                ''')
                result = cursor.fetchone()
                if result:
                    return float(result[0])
                return 1.6
        except Exception as e:
            logger.error(f"Ошибка получения цены: {e}")
            return 1.6

    def set_price_per_star(self, price: float, admin_id: int) -> bool:
        """Установка новой цены за одну звезду"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE settings 
                    SET value = ?, updated_at = ?, updated_by = ?
                    WHERE key = 'price_per_star'
                ''', (str(price), datetime.now(), admin_id))
                conn.commit()
                logger.info(f"Админ {admin_id} изменил цену на {price} руб/звезда")
                return True
        except Exception as e:
            logger.error(f"Ошибка установки цены: {e}")
            return False

    def get_price_history(self) -> List[Tuple]:
        """Получение истории изменений цены"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT value, updated_at, updated_by FROM settings WHERE key = 'price_per_star'
                ''')
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Ошибка получения истории цен: {e}")
            return []

    # === Админ методы ===
    def get_admin_stats(self):
        """Получение общей статистики для админа"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                cursor.execute("""
                    SELECT 
                        COUNT(DISTINCT user_id) as total_users,
                        COALESCE(SUM(stars_amount), 0) as total_stars,
                        COALESCE(SUM(rub_amount), 0) as total_revenue,
                        COUNT(*) as total_purchases,
                        SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending_payments,
                        COALESCE(AVG(rub_amount), 0) as avg_purchase,
                        COALESCE(AVG(stars_amount), 0) as avg_stars
                    FROM payments 
                    WHERE status = 'completed'
                """)

                result = cursor.fetchone()

                if not result or result[0] is None:
                    cursor.execute("SELECT COUNT(*) FROM payments WHERE status = 'pending'")
                    pending = cursor.fetchone()[0] or 0

                    cursor.execute("SELECT COUNT(DISTINCT user_id) FROM users")
                    total_users = cursor.fetchone()[0] or 0

                    return {
                        'total_users': total_users,
                        'total_stars': 0,
                        'total_revenue': 0,
                        'total_purchases': 0,
                        'pending_payments': pending,
                        'avg_purchase': 0,
                        'avg_stars': 0
                    }

                (total_users, total_stars, total_revenue,
                 total_purchases, pending_payments,
                 avg_purchase, avg_stars) = result

                cursor.execute("SELECT COUNT(DISTINCT user_id) FROM users")
                total_users_all = cursor.fetchone()[0] or 0

                if total_users_all > (total_users or 0):
                    total_users = total_users_all

                return {
                    'total_users': int(total_users) if total_users else 0,
                    'total_stars': int(total_stars) if total_stars else 0,
                    'total_revenue': float(total_revenue) if total_revenue else 0,
                    'total_purchases': int(total_purchases) if total_purchases else 0,
                    'pending_payments': int(pending_payments) if pending_payments else 0,
                    'avg_purchase': float(avg_purchase) if avg_purchase else 0,
                    'avg_stars': float(avg_stars) if avg_stars else 0
                }

        except Exception as e:
            logger.error(f"Ошибка при получении админ статистики: {e}")
            return {
                'total_users': 0,
                'total_stars': 0,
                'total_revenue': 0,
                'total_purchases': 0,
                'pending_payments': 0,
                'avg_purchase': 0,
                'avg_stars': 0
            }

    def get_total_stats(self) -> Dict:
        """Получение общей статистики"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                cursor.execute('''
                    SELECT 
                        COUNT(DISTINCT user_id) as total_users,
                        COALESCE(SUM(stars_amount), 0) as total_stars,
                        COALESCE(SUM(rub_amount), 0) as total_rub,
                        COUNT(*) as total_payments
                    FROM payments
                    WHERE status = 'completed'
                ''')
                stats = cursor.fetchone()

                today = datetime.now().date()
                cursor.execute('''
                    SELECT 
                        COUNT(DISTINCT user_id) as users_today,
                        COALESCE(SUM(stars_amount), 0) as stars_today,
                        COALESCE(SUM(rub_amount), 0) as rub_today
                    FROM payments 
                    WHERE DATE(created_at) = ? AND status = 'completed'
                ''', (today,))
                today_stats = cursor.fetchone()

                current_price = self.get_price_per_star()

                return {
                    'total_users': stats[0] or 0,
                    'total_stars': stats[1] or 0,
                    'total_rub': stats[2] or 0,
                    'total_payments': stats[3] or 0,
                    'users_today': today_stats[0] or 0,
                    'stars_today': today_stats[1] or 0,
                    'rub_today': today_stats[2] or 0,
                    'current_price': current_price
                }

        except Exception as e:
            logger.error(f"Ошибка получения статистики: {e}")
            return {}

    # === Методы для блокировки пользователей ===
    def ban_user(self, user_id: int, reason: str, banned_by: int = None) -> bool:
        """Блокировка пользователя"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                # Проверяем, существует ли пользователь
                cursor.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
                if not cursor.fetchone():
                    # Если пользователя нет, создаем его
                    cursor.execute('''
                        INSERT INTO users (user_id, username, first_name, last_name, registered_at)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (user_id, None, None, None, datetime.now()))

                # Добавляем в таблицу банов
                cursor.execute('''
                    INSERT OR REPLACE INTO banned_users (user_id, reason, banned_by, banned_at)
                    VALUES (?, ?, ?, ?)
                ''', (user_id, reason, banned_by, datetime.now()))

                conn.commit()
                logger.info(f"Пользователь {user_id} заблокирован. Причина: {reason}")
                return True
        except Exception as e:
            logger.error(f"Ошибка при блокировке пользователя {user_id}: {e}")
            return False

    def unban_user(self, user_id: int, unbanned_by: int = None) -> bool:
        """Разблокировка пользователя"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()

                cursor.execute('DELETE FROM banned_users WHERE user_id = ?', (user_id,))

                # Логируем разбан
                cursor.execute('''
                    INSERT INTO ban_history (user_id, action, performed_by, performed_at)
                    VALUES (?, ?, ?, ?)
                ''', (user_id, 'unban', unbanned_by, datetime.now()))

                conn.commit()
                logger.info(f"Пользователь {user_id} разблокирован")
                return True
        except Exception as e:
            logger.error(f"Ошибка при разблокировке пользователя {user_id}: {e}")
            return False

    def is_user_banned(self, user_id: int) -> bool:
        """Проверка, забанен ли пользователь"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT user_id FROM banned_users WHERE user_id = ?', (user_id,))
                return cursor.fetchone() is not None
        except Exception as e:
            logger.error(f"Ошибка при проверке бана пользователя {user_id}: {e}")
            return False

    def get_ban_info(self, user_id: int) -> dict:
        """Получение информации о бане пользователя"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT reason, banned_by, banned_at 
                    FROM banned_users 
                    WHERE user_id = ?
                ''', (user_id,))
                row = cursor.fetchone()

                if row:
                    return {
                        'reason': row[0],
                        'banned_by': row[1],
                        'banned_at': row[2]
                    }
                return {}
        except Exception as e:
            logger.error(f"Ошибка при получении информации о бане {user_id}: {e}")
            return {}

    def get_banned_users_list(self) -> list:
        """Получение списка заблокированных пользователей"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT b.user_id, u.username, b.reason, b.banned_at, b.banned_by
                    FROM banned_users b
                    LEFT JOIN users u ON b.user_id = u.user_id
                    ORDER BY b.banned_at DESC
                ''')

                banned_users = []
                for row in cursor.fetchall():
                    banned_users.append({
                        'user_id': row[0],
                        'username': row[1],
                        'reason': row[2],
                        'banned_at': row[3],
                        'banned_by': row[4]
                    })

                return banned_users
        except Exception as e:
            logger.error(f"Ошибка при получении списка забаненных пользователей: {e}")
            return []

    def get_banned_users_count(self) -> int:
        """Получение количества заблокированных пользователей"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT COUNT(*) FROM banned_users')
                return cursor.fetchone()[0]
        except Exception as e:
            logger.error(f"Ошибка при подсчете забаненных пользователей: {e}")
            return 0

    def get_user_by_username(self, username: str) -> dict:
        """Поиск пользователя по username"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT user_id, username, first_name, last_name, balance, total_stars_purchased, total_rub_spent
                    FROM users 
                    WHERE username = ?
                ''', (username,))
                row = cursor.fetchone()

                if row:
                    return {
                        'user_id': row[0],
                        'username': row[1],
                        'first_name': row[2],
                        'last_name': row[3],
                        'balance': row[4],
                        'total_stars': row[5],
                        'total_rub': row[6]
                    }
                return None
        except Exception as e:
            logger.error(f"Ошибка при поиске пользователя по username {username}: {e}")
            return None

    def get_user_by_id(self, user_id: int) -> dict:
        """Получение информации о пользователе по ID"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT user_id, username, first_name, last_name, balance, total_stars_purchased, total_rub_spent
                    FROM users 
                    WHERE user_id = ?
                ''', (user_id,))
                row = cursor.fetchone()

                if row:
                    return {
                        'user_id': row[0],
                        'username': row[1],
                        'first_name': row[2],
                        'last_name': row[3],
                        'balance': row[4],
                        'total_stars': row[5],
                        'total_rub': row[6]
                    }
                return None
        except Exception as e:
            logger.error(f"Ошибка при получении пользователя {user_id}: {e}")
            return None