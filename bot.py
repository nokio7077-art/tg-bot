import os
import logging
import re
import time
from typing import Any, Dict, Optional
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError, Conflict

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация: секреты берутся из окружения (.env), а не из кода
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
_ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()

# Ограничения Telegram Bot API
MAX_BUTTON_TEXT = 64
MAX_MESSAGE_LENGTH = 4096
MAX_RESUME_SIZE = 10 * 1024 * 1024


def _load_admin_id(raw: str) -> int:
    """Разбор ADMIN_ID из окружения с понятной ошибкой."""
    if not raw:
        raise SystemExit(
            "❌ Не задана переменная окружения ADMIN_ID.\n"
            "💡 Создайте файл .env на основе .env.example и укажите ADMIN_ID."
        )
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"❌ ADMIN_ID должен быть числом, получено: {raw!r}") from None


if not BOT_TOKEN:
    raise SystemExit(
        "❌ Не задана переменная окружения BOT_TOKEN.\n"
        "💡 Создайте файл .env на основе .env.example и укажите BOT_TOKEN."
    )

ADMIN_ID = _load_admin_id(_ADMIN_ID_RAW)

# Хранилища данных
user_data_storage: Dict[int, Dict[str, Any]] = {}
user_quiz_state: Dict[int, Dict[str, Any]] = {}

# Вопросы викторины
QUIZ_QUESTIONS = [
    {
        "question": "Почему стоимость полиса КАСКО для новичка за рулем обычно выше, чем для опытного водителя?",
        "options": [
            "У новичков дорогие машины",
            "Статистика аварийности у новичков выше",
            "Новички реже пользуются автомобилем",
        ],
        "correct_answer": 1,
        "correct_response": "✅ Верно! Страхование - это математика рисков. Статистика неумолима: у водителей со стажем менее 3 лет вероятность ДТП значительно выше.",
        # Пояснения к неверным вариантам, ключ — индекс варианта в options
        "incorrect_responses": {
            0: "❌ Не совсем. Стоимость машины влияет на цену полиса, но для новичка надбавка будет действовать независимо от марки авто.",
            2: "❌ Наоборот! Если машиной пользуются редко, риск попасть в ДТП ниже. Но новички опасны именно своей неопытностью, а не пробегом.",
        },
    },
    {
        "question": "Что такое страховая премия?",
        "options": [
            "Деньги, которые платит страховая компания",
            "Деньги, которые платит клиент за полис",
            "Бонус за продление страховки",
        ],
        "correct_answer": 1,
        "correct_response": "✅ Абсолютно верно! Страховая премия - это плата за страховую защиту, которую клиент вносит страховой компании.",
        # Пояснения к неверным вариантам, ключ — индекс варианта в options
        "incorrect_responses": {
            0: "❌ Это популярная ошибка! Деньги, которые платит страховая, называются страховое возмещение или выплата.",
            2: "❌ Звучит приятно, но нет. Бонусы бывают, но премия - это основная стоимость полиса.",
        },
    },
    {
        "question": "По договору установлена безусловная франшиза 200 BYN. Ущерб составил 1000 BYN. Сколько клиент получит?",
        "options": [
            "1000 BYN",
            "800 BYN",
            "1200 BYN",
        ],
        "correct_answer": 1,
        "correct_response": "✅ Правильно! Безусловная франшиза всегда вычитается из суммы ущерба: 1000 BYN - 200 BYN = 800 BYN.",
        # Пояснения к неверным вариантам, ключ — индекс варианта в options
        "incorrect_responses": {
            0: "❌ Не сработает. Если бы франшиза была условной и ущерб был меньше 200 BYN - да, выплаты не было бы. Но здесь ущерб больше, а франшиза - безусловная.",
            2: "❌ Хотелось бы, чтобы страховые так платили! Но нет, франшиза - это часть ущерба, которую клиент оплачивает сам.",
        },
    },
    {
        "question": "Квартира была затоплена соседями. В какой ситуации компания правомерно откажет в выплате?",
        "options": [
            "Не предоставил возможности осмотреть повреждения",
            "Виновником был несовершеннолетний",
            "Затопление произошло в ночное время",
        ],
        "correct_answer": 0,
        "correct_response": "✅ Верно! Обеспечить страховщику доступ для осмотра ущерба до начала ремонта - главная обязанность.",
        # Пояснения к неверным вариантам, ключ — индекс варианта в options
        "incorrect_responses": {
            1: "❌ Возраст виновника не важен для факта наступления страхового случая.",
            2: "❌ Время суток не влияет на обязанности страховой компании.",
        },
    },
    {
        "question": "От какого вида страхования должен быть застрахован нотариус для покрытия профессиональных ошибок?",
        "options": [
            "Ответственности владельцев опасных объектов",
            "Профессиональной ответственности",
            "Ответственности за вред третьим лицам",
        ],
        "correct_answer": 1,
        "correct_response": "✅ Точно! Страхование профессиональной ответственности защищает от исков из-за ошибок в работе.",
        # Пояснения к неверным вариантам, ключ — индекс варианта в options
        "incorrect_responses": {
            0: "❌ Это для других рисков. Опасный объект - это котельная или заправка.",
            2: "❌ Это общий вид, который покрывает травму клиента в офисе.",
        },
    },
    {
        "question": "В какой ситуации откажут в выплате по страхованию от несчастных случаев?",
        "options": [
            "Травма в ДТП как пассажир",
            "Травма при профессиональном спорте",
            "Травма из-за обострения болезни",
        ],
        "correct_answer": 2,
        "correct_response": "✅ Правильно! Страхование от несчастных случаев покрывает последствия ВНЕШНЕГО воздействия. Болезнь - внутренняя причина.",
        # Пояснения к неверным вариантам, ключ — индекс варианта в options
        "incorrect_responses": {
            0: "❌ ДТП - это классический несчастный случай, который всегда покрывается.",
            1: "❌ Если риск спорта не исключен в договоре, травма может быть страховым случаем.",
        },
    },
    {
        "question": "Что будет при двойном страховании одного объекта у двух компаний?",
        "options": [
            "Оба откажут в выплате",
            "Каждый выплатит полную сумму",
            "Выплатят пропорционально долям",
        ],
        "correct_answer": 2,
        "correct_response": "✅ Верно! Это принцип контрибуции. Страхователь не может получить сумму больше реального ущерба.",
        # Пояснения к неверным вариантам, ключ — индекс варианта в options
        "incorrect_responses": {
            0: "❌ Договоры остаются в силе, но суммарная выплата не может быть больше ущерба.",
            1: "❌ Это привело бы к необоснованному обогащению страхователя.",
        },
    },
    {
        "question": "Будет ли признан пожар страховым случаем по договору от наводнения?",
        "options": [
            "Да, как следствие наводнения",
            "Нет, ближайшая причина - пожар",
            "Да, но 50% ущерба",
        ],
        "correct_answer": 1,
        "correct_response": "✅ Абсолютно верно! Принцип causa proxima - ущерб должен быть причинен именно застрахованным риском.",
        # Пояснения к неверным вариантам, ключ — индекс варианта в options
        "incorrect_responses": {
            0: "❌ Цепочка событий длинная, но определяют именно ближайшую причину.",
            2: "❌ Процентные выплаты в таких случаях не применяются.",
        },
    },
    {
        "question": "Какой принцип нарушает клиент, скрывая болезнь при оформлении полиса?",
        "options": [
            "Принцип контрибуции",
            "Принцип добросовестности",
            "Принцип суброгации",
        ],
        "correct_answer": 1,
        "correct_response": "✅ Именно так! Принцип наивысшей добросовестности обязывает стороны быть честными.",
        # Пояснения к неверным вариантам, ключ — индекс варианта в options
        "incorrect_responses": {
            0: "❌ Контрибуция - это о другом. Этот принцип работает, когда один риск застрахован у нескольких компаний.",
            2: "❌ Суброгация - право страховщика требовать компенсацию с виновника.",
        },
    },
]

PRIZES = {
    (0, 2): ("🍫 фирменная шоколадка", "Страхование - сложная тема, но вы сделали первый шаг! Заглядывайте к нам за полисами - разберем все тонкости лично!"),
    (3, 4): ("📱 стикерпак", "Есть базовое понимание, но еще есть куда расти! Мы поможем разобраться во всех страховых вопросах."),
    (5, 6): ("✒️ фирменная ручка", "Хороший результат! Вы хорошо ориентируетесь в основах. Приходите за полисом - и ручка будет исправно служить при подписании договора!"),
    (7, 8): ("📓 стильный блокнот", "Отличные знания! Вы явно в теме и умеете отличать франшизу от суброгации. Блокнот пригодится для записи выгодных условий наших полисов!"),
    (9, 10): ("🛍️ модный шопер", "Идеальный результат! Вы - настоящий эксперт в страховании! Такому профи наш шопер точно пригодится. Ждем вас за заслуженным призом.")
}

LEVELS = {
    (0, 2): "🔴 Начальный уровень",
    (3, 4): "🟡 Базовый уровень",
    (5, 6): "🟢 Средний уровень", 
    (7, 8): "🔵 Продвинутый уровень",
    (9, 10): "🟣 Экспертный уровень"
}

# Утилиты
def is_valid_email(email: str) -> bool:
    """Проверка валидности email"""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

def format_time_difference(seconds: float) -> str:
    """Форматирование времени в читаемый вид"""
    minutes = int(seconds // 60)
    seconds = int(seconds % 60)
    return f"{minutes} мин {seconds} сек" if minutes > 0 else f"{seconds} сек"

def get_prize_info(score: int) -> tuple:
    """Получение информации о призе по количеству баллов"""
    for (min_score, max_score), (prize, message) in PRIZES.items():
        if min_score <= score <= max_score:
            return prize, message
    return "🎁 приз", "Спасибо за участие!"

def get_level(score: int) -> str:
    """Получение уровня знаний по количеству баллов"""
    for (min_score, max_score), level in LEVELS.items():
        if min_score <= score <= max_score:
            return level
    return "⚪ Неопределенный уровень"

async def send_to_admin(context: ContextTypes.DEFAULT_TYPE, message: str,
                        document_id: Optional[str] = None, caption: Optional[str] = None) -> None:
    """Отправка сообщения администратору"""
    try:
        if document_id:
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=document_id,
                caption=caption
            )
        else:
            await context.bot.send_message(chat_id=ADMIN_ID, text=message)
    except TelegramError as e:
        logger.error(f"Ошибка отправки администратору: {e}")

async def send_quiz_results_to_admin(context: ContextTypes.DEFAULT_TYPE, user_id: int, username: str, 
                                   score: int, total_questions: int, prize: str, time_taken: str):
    """Отправка результатов викторины администратору"""
    try:
        percentage = (score / total_questions) * 100
        level = get_level(score)
        
        message = (
            "🎯 НОВЫЕ РЕЗУЛЬТАТЫ ВИКТОРИНЫ\n\n"
            f"👤 Пользователь: @{username or 'без username'}\n"
            f"🆔 ID: {user_id}\n"
            f"📊 Результат: {score} из {total_questions} ({percentage:.1f}%)\n"
            f"⏱️ Время прохождения: {time_taken}\n"
            f"🏆 Уровень: {level}\n"
            f"🎁 Приз: {prize}\n"
            f"📅 Завершено: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"
        )
        
        await send_to_admin(context, message)
        
    except Exception as e:
        logger.error(f"Ошибка отправки результатов администратору: {e}")

# Обработчики ошибок
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик ошибок"""
    logger.error("Ошибка при обработке обновления: %s", context.error, exc_info=context.error)

    # update может быть не объектом Update, поэтому обращаемся к полям осторожно
    if not isinstance(update, Update):
        return

    user = update.effective_user
    logger.warning("Ошибка у пользователя %s", user.id if user else "неизвестен")

    query = update.callback_query
    if query is None:
        return

    try:
        await query.edit_message_text(
            "❌ Что-то пошло не так. Пожалуйста, начните заново.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 В главное меню", callback_data="back_to_menu")]]
            )
        )
    except TelegramError as e:
        logger.error(f"Не удалось сообщить пользователю об ошибке: {e}")

async def conflict_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик конфликтов"""
    if isinstance(context.error, Conflict):
        logger.warning("Обнаружен конфликт - вероятно, запущено несколько экземпляров бота")
        await send_to_admin(context, "⚠️ Обнаружен конфликт! Проверьте, что запущен только один экземпляр бота.")

# Основные обработчики
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start"""
    user_id = update.effective_user.id
    
    # Сбрасываем состояние пользователя
    user_data_storage.pop(user_id, None)
    
    welcome_message = (
        "🛡️ ДОБРО ПОЖАЛОВАТЬ В БЕЛНЕФТЕСТРАХ\n\n"
        "🏢 Официальный бот страховой компании\n\n"
        "✨ Что вас ждет:\n"
        "🎮 Увлекательная викторина о страховании\n"
        "🎁 Ценные призы за участие\n"
        "💼 Информация о карьерных возможностях\n"
        "🏢 Знакомство с нашей компанией\n\n"
        "Выберите действие:"
    )
    
    keyboard = [
        [InlineKeyboardButton("🎮 Начать викторину", callback_data="start_quiz")],
        [InlineKeyboardButton("🏢 Узнать о нас", callback_data="about_us")],
        [InlineKeyboardButton("💼 Карьера у нас", callback_data="career")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text=welcome_message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            text=welcome_message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message: str = None) -> None:
    """Показ главного меню"""
    keyboard = [
        [InlineKeyboardButton("🎮 Начать викторину", callback_data="start_quiz")],
        [InlineKeyboardButton("🏢 Узнать о нас", callback_data="about_us")],
        [InlineKeyboardButton("💼 Карьера у нас", callback_data="career")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    menu_message = message or "🎯 *Главное меню*\n\nВыберите опцию:"
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text=menu_message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    elif update.effective_message:
        await update.effective_message.reply_text(
            text=menu_message,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик нажатий на кнопки"""
    query = update.callback_query
    await query.answer()

    try:
        if query.data == "start_quiz":
            await start_quiz(update, context)
        elif query.data == "about_us":
            await about_us(update, context)
        elif query.data == "career":
            await career_info(update, context)
        elif query.data == "back_to_menu":
            await show_main_menu(update, context)
        elif query.data == "leave_contacts":
            await start_contact_collection(update, context)
        elif query.data.startswith("answer_"):
            await handle_quiz_answer(update, context)
        elif query.data == "next_question":
            await ask_next_question(update, context)
        elif query.data == "continue_quiz":
            await continue_quiz(update, context)
        elif query.data == "skip_resume":
            await skip_resume(update, context)
        else:
            logger.warning(f"Неизвестный callback_data: {query.data!r}")
            await show_main_menu(update, context, "❓ Эта кнопка устарела. Выберите действие заново:")
    except Exception as e:
        logger.exception(f"Ошибка в button_handler: {e}")
        try:
            await query.edit_message_text(
                "❌ Произошла ошибка. Пожалуйста, попробуйте снова.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🏠 В главное меню", callback_data="back_to_menu")]]
                )
            )
        except TelegramError as edit_error:
            logger.error(f"Не удалось отредактировать сообщение об ошибке: {edit_error}")

async def about_us(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Информация о компании"""
    message = (
        "🏢 *Белнефтестрах* — это:\n\n"
        "✅ Одна из ведущих страховых компаний Беларуси\n"
        "🤝 Надежный партнер для физических и юридических лиц\n"
        "📊 Широкий спектр страховых услуг\n"
        "💡 Современные технологии и индивидуальный подход\n\n"
        "🛡️ Мы защищаем то, что важно именно для тебя!"
    )
    
    keyboard = [
        [InlineKeyboardButton("🎮 Пройти викторину", callback_data="start_quiz")],
        [InlineKeyboardButton("🏠 В главное меню", callback_data="back_to_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.callback_query.edit_message_text(
        text=message,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def career_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Информация о карьере"""
    message = (
        "🚀 *Мы растем и ищем талантливых людей!* В «Белнефтестрах» тебя ждет:\n\n"
        "💼 Стабильная работа в надежной компании\n"
        "📈 Профессиональное развитие и обучение\n"
        "🏙️ Современный офис в центре города\n"
        "👥 Дружный коллектив и забота о сотрудниках\n\n"
        "💌 Хочешь узнать о доступных вакансиях? Оставь свои контакты, и наш HR свяжется с тобой!"
    )
    
    keyboard = [
        [InlineKeyboardButton("📝 Оставить контакты", callback_data="leave_contacts")],
        [InlineKeyboardButton("🏠 В главное меню", callback_data="back_to_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.callback_query.edit_message_text(
        text=message,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

# Обработчики контактных данных
async def start_contact_collection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Начало сбора контактных данных"""
    user_id = update.callback_query.from_user.id
    user_data_storage[user_id] = {"step": "waiting_for_name"}
    
    await update.callback_query.edit_message_text(
        text="👋 *Давайте познакомимся!*\n\nПожалуйста, напишите ваше:\n\n*ФИО (как в паспорте)*",
        parse_mode='Markdown'
    )

async def handle_contact_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка контактной информации"""
    user_id = update.message.from_user.id
    text = (update.message.text or "").strip()

    if user_id not in user_data_storage:
        await handle_user_not_in_process(update, context)
        return

    current_step = user_data_storage[user_id].get("step")

    if current_step == "waiting_for_name":
        await handle_name_input(update, context, text)
    elif current_step == "waiting_for_age":
        await handle_age_input(update, context, text)
    elif current_step == "waiting_for_email":
        await handle_email_input(update, context, text)
    elif current_step == "waiting_for_resume":
        # Текст вместо файла означает отправку анкеты без резюме
        await handle_text_resume(update, context, user_id)
    else:
        logger.warning(f"Неизвестный шаг анкеты {current_step!r} у пользователя {user_id}")
        user_data_storage.pop(user_id, None)
        await handle_user_not_in_process(update, context)

async def handle_user_not_in_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка случая, когда пользователь не в процессе"""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        logger.debug("Пропускаем обновление без сообщения или пользователя")
        return

    user_id = user.id

    if user_id in user_quiz_state and not user_quiz_state[user_id].get('quiz_completed', False):
        keyboard = [
            [InlineKeyboardButton("➡️ Продолжить викторину", callback_data="continue_quiz")],
            [InlineKeyboardButton("🏠 В главное меню", callback_data="back_to_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await message.reply_text(
            "❌ Я не понимаю эту команду во время викторины. Хотите продолжить?",
            reply_markup=reply_markup
        )
    else:
        await show_main_menu(update, context, "❌ Я не понимаю эту команду. Пожалуйста, используйте кнопки меню для навигации.")

async def handle_name_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Обработка ввода имени"""
    user_id = update.message.from_user.id
    
    if len(text) < 2:
        await update.message.reply_text("❌ Пожалуйста, введите корректное ФИО (минимум 2 символа):")
        return
    
    user_data_storage[user_id]["full_name"] = text
    user_data_storage[user_id]["step"] = "waiting_for_age"
    await update.message.reply_text("✅ Спасибо! Теперь укажите ваш:\n\n*Возраст (полных лет, от 18 до 80)*", parse_mode='Markdown')

async def handle_age_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Обработка ввода возраста"""
    user_id = update.message.from_user.id
    
    if not text.isdigit():
        await update.message.reply_text("❌ Пожалуйста, введите возраст числом:")
        return
    
    age = int(text)
    if age < 18 or age > 80:
        await update.message.reply_text("❌ Возраст должен быть от 18 до 80 лет. Пожалуйста, введите корректный возраст:")
        return
    
    user_data_storage[user_id]["age"] = age
    user_data_storage[user_id]["step"] = "waiting_for_email"
    await update.message.reply_text("✅ Спасибо! Теперь укажите ваш E-mail для связи:")

async def handle_email_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Обработка ввода email"""
    user_id = update.message.from_user.id
    
    if not is_valid_email(text):
        await update.message.reply_text("❌ Пожалуйста, введите корректный email адрес (например: example@mail.com):")
        return
    
    user_data_storage[user_id]["email"] = text
    user_data_storage[user_id]["step"] = "waiting_for_resume"
    
    keyboard = [
        [InlineKeyboardButton("📤 Отправить без резюме", callback_data="skip_resume")],
        [InlineKeyboardButton("🏠 В главное меню", callback_data="back_to_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "📎 *Последний шаг!*\n\nОтправьте ваше резюме (pdf/doc/docx до 10 МБ) или нажмите кнопку ниже:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

def build_application_message(user_info: Dict[str, Any], user_id: int, username: Optional[str]) -> str:
    """Текст заявки на вакансию для администратора"""
    return (
        "📋 НОВАЯ ЗАЯВКА НА ВАКАНСИЮ:\n\n"
        f"👤 ФИО: {user_info.get('full_name', 'Не указано')}\n"
        f"🎂 Возраст: {user_info.get('age', 'Не указан')}\n"
        f"📧 Email: {user_info.get('email', 'Не указан')}\n"
        f"📎 Резюме: {user_info.get('resume_file_name', 'Не прикреплено')}\n"
        f"🆔 ID пользователя: {user_id}\n"
        f"👤 Username: @{username or 'Не указан'}"
    )


async def skip_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пропуск отправки резюме"""
    query = update.callback_query
    user_id = query.from_user.id
    
    if user_id not in user_data_storage:
        # Состояние анкеты истекло — сообщаем об этом вместо молчания
        await show_main_menu(
            update, context,
            "⌛️ Анкета устарела. Пожалуйста, заполните её заново."
        )
        return

    user_data_storage[user_id].update({
        "has_resume": False,
        "resume_file_name": "Не прикреплено"
    })

    user_info = user_data_storage[user_id]
    await send_to_admin(
        context,
        build_application_message(user_info, user_id, query.from_user.username)
    )

    keyboard = [
        [InlineKeyboardButton("🎮 Пройти викторину", callback_data="start_quiz")],
        [InlineKeyboardButton("🏠 В главное меню", callback_data="back_to_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "🎉 *Благодарим за интерес к нашей компании!*\n\n"
        "✅ Ваши данные сохранены. Наш HR-специалист изучит вашу анкету и свяжется "
        "с вами в ближайшее время по электронной почте.\n\n"
        "🎁 А пока можете пройти нашу увлекательную викторину и выиграть приз!",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

    # Очищаем временные данные
    user_data_storage.pop(user_id, None)

async def handle_message_or_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка текстовых сообщений и документов для резюме"""
    user_id = update.message.from_user.id
    
    if user_id not in user_data_storage or user_data_storage[user_id].get("step") != "waiting_for_resume":
        await handle_user_not_in_process(update, context)
        return
    
    if update.message.document:
        await handle_document(update, context, user_id)
    elif update.message.text:
        await handle_text_resume(update, context, user_id)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Обработка документа-резюме"""
    document = update.message.document
    file_size = document.file_size or 0

    if file_size > MAX_RESUME_SIZE:
        await update.message.reply_text("❌ Файл слишком большой! Максимальный размер - 10 МБ.")
        return

    # Telegram не гарантирует наличие имени файла
    file_name = document.file_name or ""
    if not file_name.lower().endswith(('.pdf', '.doc', '.docx')):
        await update.message.reply_text("❌ Неподдерживаемый формат! Отправьте PDF, DOC или DOCX.")
        return

    user_data_storage[user_id].update({
        "resume_file_id": document.file_id,
        "resume_file_name": file_name,
        "has_resume": True
    })
    
    await process_final_step(update, context, user_id)

async def handle_text_resume(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Обработка текстового сообщения вместо резюме"""
    user_data_storage[user_id].update({
        "has_resume": False,
        "resume_file_name": "Не прикреплено"
    })
    
    await process_final_step(update, context, user_id)

async def process_final_step(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Финальная обработка анкеты"""
    user_info = user_data_storage[user_id]

    await send_to_admin(
        context,
        build_application_message(user_info, user_id, update.message.from_user.username)
    )
    
    # Отправляем файл резюме если есть
    if user_info.get("has_resume") and user_info.get("resume_file_id"):
        try:
            await send_to_admin(
                context, 
                "", 
                document_id=user_info["resume_file_id"],
                caption=f"📎 Резюме от {user_info.get('full_name', 'пользователя')}"
            )
        except TelegramError as e:
            logger.error(f"Ошибка отправки резюме: {e}")
    
    # Подтверждение пользователю
    keyboard = [
        [InlineKeyboardButton("🎮 Пройти викторину", callback_data="start_quiz")],
        [InlineKeyboardButton("🏠 В главное меню", callback_data="back_to_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🎉 *Благодарим за интерес к нашей компании!*\n\n"
        "✅ Ваши данные сохранены. Наш HR-специалист свяжется с вами.\n\n"
        "🎁 А пока можете пройти нашу увлекательную викторину и выиграть приз!",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    
    # Очищаем данные
    user_data_storage.pop(user_id, None)

# Функции викторины
def get_quiz_state(user_id: int) -> Dict[str, Any]:
    """Безопасное получение состояния викторины"""
    if user_id not in user_quiz_state:
        # Создаем новое состояние, если не существует
        user_quiz_state[user_id] = {
            'current_question': 0,
            'score': 0,
            'quiz_completed': False
        }
    return user_quiz_state[user_id]

async def quiz_session_expired(update: Update) -> None:
    """Сообщение об истёкшей сессии викторины"""
    await update.callback_query.edit_message_text(
        "⌛️ Сессия викторины устарела. Пожалуйста, начните заново.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🎮 Начать викторину", callback_data="start_quiz")]]
        )
    )


async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Начало викторины"""
    user_id = update.callback_query.from_user.id
    
    user_quiz_state[user_id] = {
        'current_question': 0,
        'score': 0,
        'quiz_completed': False,
        'start_time': time.time()
    }
    
    await ask_question(update, context)

async def continue_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Продолжение викторины"""
    user_id = update.callback_query.from_user.id
    
    if user_id not in user_quiz_state:
        await start_quiz(update, context)
        return

    await ask_question(update, context)

async def ask_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Задание вопроса викторины"""
    user_id = update.callback_query.from_user.id
    
    # Используем безопасный доступ
    quiz_state = get_quiz_state(user_id)
    current_question_index = quiz_state['current_question']
    
    if current_question_index >= len(QUIZ_QUESTIONS):
        await finish_quiz(update, context)
        return
    
    question_data = QUIZ_QUESTIONS[current_question_index]
    
    keyboard = []
    for i, option in enumerate(question_data['options']):
        button_text = option[:MAX_BUTTON_TEXT]
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"answer_{i}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message = f"❓ *Вопрос {current_question_index + 1} из {len(QUIZ_QUESTIONS)}*\n\n{question_data['question']}"
    
    await update.callback_query.edit_message_text(
        text=message,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def handle_quiz_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка ответа на вопрос викторины"""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Проверяем, существует ли состояние викторины для пользователя
    if user_id not in user_quiz_state:
        await query.edit_message_text(
            "❌ Сессия викторины устарела или не найдена. Пожалуйста, начните заново.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎮 Начать викторину", callback_data="start_quiz")]])
        )
        return
    
    try:
        answer_index = int(query.data.split('_')[1])
    except (IndexError, ValueError):
        logger.warning(f"Некорректный callback_data ответа: {query.data!r}")
        await quiz_session_expired(update)
        return

    current_question_index = user_quiz_state[user_id]['current_question']

    # Состояние может указывать за пределы списка вопросов
    if not 0 <= current_question_index < len(QUIZ_QUESTIONS):
        await quiz_session_expired(update)
        return

    question_data = QUIZ_QUESTIONS[current_question_index]

    if not 0 <= answer_index < len(question_data['options']):
        logger.warning(f"Ответ вне диапазона: {answer_index}")
        await quiz_session_expired(update)
        return

    is_correct = (answer_index == question_data['correct_answer'])

    if is_correct:
        user_quiz_state[user_id]['score'] += 1
        response_text = question_data['correct_response']
    else:
        # Пояснения хранятся по индексу варианта, поэтому подмена невозможна
        response_text = question_data['incorrect_responses'].get(answer_index, '❌ Неправильно!')

    if len(response_text) > MAX_MESSAGE_LENGTH:
        response_text = response_text[:MAX_MESSAGE_LENGTH - 3] + "..."
    
    keyboard = [[InlineKeyboardButton("➡️ Следующий вопрос", callback_data="next_question")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text=response_text,
        reply_markup=reply_markup
    )

async def ask_next_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Переход к следующему вопросу"""
    user_id = update.callback_query.from_user.id

    if user_id not in user_quiz_state:
        await quiz_session_expired(update)
        return

    user_quiz_state[user_id]['current_question'] += 1
    await ask_question(update, context)

async def finish_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Завершение викторины и показ результатов"""
    query = update.callback_query
    user_id = query.from_user.id
    
    if user_id not in user_quiz_state:
        await quiz_session_expired(update)
        return

    quiz_data = user_quiz_state[user_id]
    score = quiz_data['score']
    total_questions = len(QUIZ_QUESTIONS)
    start_time = quiz_data.get('start_time', time.time())
    end_time = time.time()
    
    quiz_data['quiz_completed'] = True
    
    # Получаем информацию о призе
    prize, prize_message = get_prize_info(score)
    time_taken = format_time_difference(end_time - start_time)
    
    # Формируем сообщение для пользователя
    message = (
        f"🎉 *Поздравляем! Вы прошли викторину!*\n\n"
        f"📊 Ваш результат: {score} из {total_questions} правильных ответов\n\n"
        f"🎁 Ваш приз - {prize}!\n\n"
        f"{prize_message}"
    )
    
    # Отправляем результаты администратору
    username = query.from_user.username
    await send_quiz_results_to_admin(context, user_id, username, score, total_questions, prize, time_taken)
    
    keyboard = [[InlineKeyboardButton("🏠 В главное меню", callback_data="back_to_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text=message,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def handle_unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик неизвестных сообщений"""
    await handle_user_not_in_process(update, context)

# Главная функция
def main():
    """Основная функция запуска бота"""
    print("🤖 Запускаем бота...")
    
    # Создаем приложение
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Добавляем обработчики ошибок
    application.add_error_handler(error_handler)
    application.add_error_handler(conflict_handler)
    
    # Регистрируем обработчики команд.
    # Порядок важен: в одной группе срабатывает только первый подходящий обработчик,
    # поэтому общий filters.ALL идёт последним.
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_message_or_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_contact_info))
    application.add_handler(MessageHandler(filters.ALL, handle_unknown_message))
    
    # Запускаем бота
    print("✅ Бот запущен и работает...")
    print("⏹️  Для остановки нажмите Ctrl+C")
    
    try:
        application.run_polling()
    except Conflict as e:
        print(f"❌ Ошибка: уже запущен другой экземпляр бота! {e}")
        print("💡 Решение: остановите все другие экземпляры и запустите заново")
    except KeyboardInterrupt:
        print("\n🛑 Бот остановлен пользователем")
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
    finally:
        print("👋 До свидания!")

if __name__ == "__main__":
    main()