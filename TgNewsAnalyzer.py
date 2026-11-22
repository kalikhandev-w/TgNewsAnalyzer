import sys
import os
import re
import math
import traceback
import multiprocessing

# The frozen app re-runs itself for multiprocessing helpers (tqdm's lock starts the resource tracker on the
# first encode); divert them here, or each one opens a second app on the same Telegram session
multiprocessing.freeze_support()
import pandas as pd
import spacy
import asyncio
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QVBoxLayout, 
    QWidget, QFileDialog, QTableWidget, QTableWidgetItem, 
    QProgressBar, QDateEdit, QLabel, QHBoxLayout, QHeaderView,
    QDialog, QLineEdit, QMessageBox, QProgressDialog, QTextEdit, QCheckBox,
    QInputDialog
)
from PyQt5.QtCore import QDate, pyqtSignal, Qt, QTimer
from PyQt5.QtGui import QIcon, QPixmap, QPainter
from qr_render import qr_matrix
from docx import Document
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
from qasync import QEventLoop, asyncSlot
from datetime import datetime, timedelta, timezone
from timeutils import KZ_TZ, to_kz, kz_date
from links import extract_message_id, extract_channel_name, find_telegram_links
from matching import combined_score, greedy_assign
from onboarding_logic import normalize_phone, run_self_check
from connectivity import with_reconnect
from model_loader import load_models, bge_total_bytes, dir_size_bytes, bge_cache_folder
from sentence_transformers import SentenceTransformer, util


import app_paths
from credentials import get_api_credentials

app_paths.ensure_dirs()
try:
    API_ID, API_HASH = get_api_credentials()
except RuntimeError as e:


    print(f"CONFIG ERROR: {e}")
    raise

SESSION_NAME = app_paths.session_file()
PROXY = None

# Channels to compare with
channels = {
    'Nur': 'newsnurkz',
    'Atameken': 'atamekenbusiness',
    'ZTB': 'ztb_qaz',
    'Sputnik': 'sputnikKZ'
}
target_channel = 'zakonkz'

similarity_threshold = 0.5  # Порог сходства для USER-bge-m3 (нормализованные эмбеддинги)


global nlp, sentence_model
nlp = None
sentence_model = None

def preprocess_text(text):
    """Очистка текста от URL, username, хештегов и другого шума."""
    if not text:
        return ""
    # Удаление URL-адресов
    text = re.sub(r'https?://\S+|www\.\S+', '', text)
    # Удаление упоминаний (@username)
    text = re.sub(r'@\w+', '', text)
    # Удаление хештегов (#tag)
    text = re.sub(r'#\w+', '', text)
    # Удаление лишних пробелов, которые могли остаться после удаления
    text = re.sub(r'\s+', ' ', text).strip()
    return text

class TelegramAuthDialog(QDialog):
    """Dialog for Telegram authentication"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.client = None
        self.phone = None
        self.session_check_timer = None
        self.check_session_task = None  # Добавляем отслеживание задачи проверки сессии
        self.was_accepted = False  # Добавляем флаг для отслеживания результата
        self.auth_future = None  # Добавляем future для ожидания результата
        self.telegram_semaphore = asyncio.Semaphore(1)  # Только 1 операция с Telegram API одновременно
        self.initUI()
        
        # Проверяем существующую сессию при создании диалога
        self.check_existing_session()
        
    def initUI(self):
        """Initialize the UI"""
        self.setWindowTitle("Авторизация Telegram")
        self.setFixedSize(400, 250)
        
        layout = QVBoxLayout()
        
        # Phone number input
        phone_layout = QHBoxLayout()
        self.phone_input = QLineEdit()
        self.phone_input.setPlaceholderText("+7XXXXXXXXXX")
        self.send_code_btn = QPushButton("Отправить код")
        # Используем обычный метод для слота вместо lambda
        self.send_code_btn.clicked.connect(self.on_send_code_clicked)
        phone_layout.addWidget(QLabel("Номер телефона:"))
        phone_layout.addWidget(self.phone_input)
        phone_layout.addWidget(self.send_code_btn)
        layout.addLayout(phone_layout)
        
        # Code input
        code_layout = QHBoxLayout()
        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText("Введите код")
        self.code_input.setEnabled(False)
        self.verify_code_btn = QPushButton("Проверить код")
        # Используем обычный метод для слота вместо lambda
        self.verify_code_btn.clicked.connect(self.on_verify_code_clicked)
        self.verify_code_btn.setEnabled(False)
        code_layout.addWidget(QLabel("Код подтверждения:"))
        code_layout.addWidget(self.code_input)
        code_layout.addWidget(self.verify_code_btn)
        layout.addLayout(code_layout)
        
        # Skip session check button
        self.skip_check_btn = QPushButton("Пропустить проверку сессии")
        self.skip_check_btn.clicked.connect(self.skip_session_check)
        layout.addWidget(self.skip_check_btn)
        
        # Status label
        self.status_label = QLabel("Проверка существующей сессии... (10 секунд)")
        layout.addWidget(self.status_label)
        
        self.setLayout(layout)
    
    # Новые методы для обработки сигналов от кнопок
    def on_send_code_clicked(self):
        """Обработчик нажатия кнопки 'Отправить код'"""
        print("Кнопка 'Отправить код' нажата")
        self.status_label.setText("Подготовка к отправке кода...")
        # Запускаем асинхронную задачу в корректном контексте
        asyncio.create_task(self._async_send_code())
        
    def on_verify_code_clicked(self):
        """Обработчик нажатия кнопки 'Проверить код'"""
        print("Кнопка 'Проверить код' нажата")
        self.status_label.setText("Подготовка к проверке кода...")
        # Запускаем асинхронную задачу в корректном контексте
        asyncio.create_task(self._async_verify_code())
    
    async def _async_send_code(self):
        """Асинхронная отправка кода"""
        try:
            print("Запуск _async_send_code")
            # Создаем таймер для контроля выполнения
            send_code_timer = QTimer()
            send_code_timer.setSingleShot(True)
            send_code_timer.timeout.connect(lambda: self._handle_timeout("отправки кода"))
            send_code_timer.start(45000)  # 45 секунд таймаута (увеличенное время)
            
            # Запускаем отправку кода
            print("Вызов функции send_code")
            await self.send_code()
            
            # Если мы дошли до этой точки, останавливаем таймер
            print("Функция send_code завершилась успешно, останавливаем таймер")
            send_code_timer.stop()
        except Exception as e:
            print(f"Ошибка при отправке кода в _async_send_code: {str(e)}")
            self.status_label.setText(f"Ошибка: {str(e)}")
            # Повторно включаем поле ввода и кнопку в случае ошибки
            self.phone_input.setEnabled(True)
            self.send_code_btn.setEnabled(True)
            # Отклоняем диалог в случае критической ошибки
            self.reject()

    def _handle_timeout(self, operation_name):
        """Обработчик таймаута операции"""
        print(f"Таймаут операции: {operation_name}")
        error_msg = f"{operation_name.capitalize()} заняла слишком много времени. Возможные причины:\n"\
                    "1. Проблемы с подключением к интернету\n"\
                    "2. Перегрузка серверов Telegram\n"\
                    "3. Ограничения API Telegram\n\n"\
                    "Рекомендуем:\n"\
                    "- Проверить подключение к интернету\n"\
                    "- Попробовать позже\n"\
                    "- Проверить правильность введенного номера телефона"
        
        QMessageBox.warning(self, "Превышено время ожидания", error_msg)
        self.status_label.setText(f"Ошибка: превышено время ожидания при {operation_name}")
        
        # Повторно включаем поля ввода
        self.phone_input.setEnabled(True)
        self.send_code_btn.setEnabled(True)
    
    def skip_session_check(self):
        """Skip session check and enable manual auth"""
        if self.session_check_timer:
            self.session_check_timer.stop()
            
        self.skip_check_btn.hide()
        self.status_label.setText("Проверка сессии пропущена. Введите номер телефона.")
        self.phone_input.setEnabled(True)
        self.send_code_btn.setEnabled(True)
    
    def check_existing_session(self):
        """Check if there is an existing session"""
        # Отключаем поля ввода на время проверки
        self.phone_input.setEnabled(False)
        self.send_code_btn.setEnabled(False)
        
        # Запускаем таймер для ограничения времени проверки
        self.session_check_timer = QTimer()
        self.session_check_timer.timeout.connect(self.session_check_timeout)
        self.session_check_timer.start(30000)  # Увеличиваем до 30 секунд таймаут
        
        # Запускаем асинхронную проверку и сохраняем задачу
        self.check_session_task = asyncio.create_task(self._check_session())
        
    def session_check_timeout(self):
        """Called when session check times out"""
        if self.check_session_task and not self.check_session_task.done():
            print("Отмена задачи проверки сессии из-за таймаута")
            self.check_session_task.cancel()
            
        self.skip_session_check()
        self.status_label.setText("Время проверки сессии истекло. Введите номер телефона вручную.")
        
    async def _check_session(self):
        """Actual async check for existing session"""
        try:
            if not self.isVisible():  # Проверяем, что диалог все еще отображается
                print("Диалог уже закрыт, отменяем проверку сессии")
                return
                
            self.status_label.setText("Подключение к Telegram API...")
            print("Инициализация клиента для проверки сессии")
            # Создаем клиент с прокси, если он указан
            if PROXY:
                print("Используется прокси для подключения")
                self.client = TelegramClient(SESSION_NAME, API_ID, API_HASH, proxy=PROXY)
            else:
                self.client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
            
            try:
                print("Подключение к Telegram API с таймаутом 30 секунд")
                await asyncio.wait_for(self.client.connect(), 30)  # Увеличиваем таймаут подключения до 30 секунд
                print("Подключение успешно")
            except asyncio.TimeoutError:
                print("Превышено время ожидания при подключении")
                raise TimeoutError("Не удалось подключиться к серверам Telegram. Проверьте интернет-соединение или попробуйте использовать прокси.")
            except Exception as e:
                print(f"Ошибка при подключении: {str(e)}")
                raise Exception(f"Ошибка подключения к Telegram API: {str(e)}")
            
            if not self.isVisible():  # Проверяем после каждой длительной операции
                return
                
            self.status_label.setText("Проверка авторизации...")
            print("Проверка авторизации с таймаутом 20 секунд")
            try:
                is_authorized = await asyncio.wait_for(self.client.is_user_authorized(), 20)  # Увеличиваем таймаут проверки до 20 секунд
                print(f"Статус авторизации: {is_authorized}")
            except asyncio.TimeoutError:
                print("Превышено время ожидания при проверке авторизации")
                raise TimeoutError("Превышено время ожидания при проверке статуса авторизации")
            except Exception as e:
                print(f"Ошибка при проверке авторизации: {str(e)}")
                raise Exception(f"Ошибка проверки авторизации: {str(e)}")
                
            if is_authorized:
                if not self.isVisible():
                    return
                    
                print("Сессия подтверждена, авторизация успешна")
                self.status_label.setText("Найдена существующая сессия. Авторизация успешна!")
                
                # Останавливаем таймер, если проверка выполнена успешно
                if self.session_check_timer:
                    self.session_check_timer.stop()
                    
                self.skip_check_btn.hide()
                QTimer.singleShot(1000, self.accept)  # Закрыть диалог через 1 секунду
                return
            
            # Проверка не нашла существующую сессию
            if not self.isVisible():
                return
            
            print("Существующая сессия не найдена")
            self.status_label.setText("Существующая сессия не найдена. Введите номер телефона.")
            if self.session_check_timer:
                self.session_check_timer.stop()
                
            self.skip_check_btn.hide()
            self.phone_input.setEnabled(True)
            self.send_code_btn.setEnabled(True)
                
        except Exception as e:
            if not self.isVisible():
                return
                
            print(f"Ошибка при проверке сессии: {str(e)}")
            self.status_label.setText("Ошибка при проверке сессии, введите номер телефона")
            self.phone_input.setEnabled(True)
            self.send_code_btn.setEnabled(True)
            self.skip_check_btn.hide()
            
            # Останавливаем таймер при ошибке
            if self.session_check_timer:
                self.session_check_timer.stop()

    async def send_code(self):
        """Send authentication code to the phone number"""
        print("Функция send_code вызвана")
        self.phone = self.phone_input.text().strip()
        if not self.phone:
            QMessageBox.warning(self, "Ошибка", "Введите номер телефона")
            return
            
        try:
            self.status_label.setText("Отправка кода...")
            

            self.phone = normalize_phone(self.phone)
            self.phone_input.setText(self.phone)
            
            print(f"Используемый номер телефона: {self.phone}")
            
            # Создаем нового клиента каждый раз
            print("Создание нового клиента Telegram")
            if self.client:
                print("Отключение предыдущего клиента")
                try:
                    await asyncio.wait_for(self.client.disconnect(), 5)
                    print("Предыдущий клиент отключен")
                except Exception as e:
                    print(f"Ошибка при отключении предыдущего клиента: {str(e)}")
                    # Продолжаем, даже если не удалось отключить предыдущий клиент
            
            print(f"Инициализация клиента с API_ID={API_ID}, API_HASH={API_HASH[:4]}..., SESSION_NAME={SESSION_NAME}")
            # Создаем клиент с прокси, если он указан
            if PROXY:
                print("Используется прокси для подключения")
                self.client = TelegramClient(SESSION_NAME, API_ID, API_HASH, proxy=PROXY)
            else:
                self.client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
            
            # Устанавливаем короткий таймаут для подключения
            print("Попытка подключения к Telegram API...")
            self.status_label.setText("Подключение к Telegram API...")
            try:
                connect_timeout = 30  # Увеличиваем до 30 секунд таймаут
                print(f"Ожидание подключения (таймаут {connect_timeout} сек)")
                await asyncio.wait_for(self.client.connect(), connect_timeout)
                print("Подключение успешно")
            except asyncio.TimeoutError:
                print("Превышено время ожидания при подключении")
                raise TimeoutError("Не удалось подключиться к серверам Telegram. Проверьте ваше интернет-соединение или попробуйте использовать прокси.")
            except Exception as e:
                print(f"Ошибка при подключении: {str(e)}")
                raise Exception(f"Ошибка подключения к Telegram API: {str(e)}")
            
            # Проверяем авторизацию
            print("Проверка авторизации")
            self.status_label.setText("Проверка авторизации...")
            try:
                auth_timeout = 20  # Увеличиваем до 20 секунд таймаут
                print(f"Ожидание проверки авторизации (таймаут {auth_timeout} сек)")
                is_authorized = await asyncio.wait_for(self.client.is_user_authorized(), auth_timeout)
                print(f"Статус авторизации: {is_authorized}")
            except asyncio.TimeoutError:
                print("Превышено время ожидания при проверке авторизации")
                raise TimeoutError("Превышено время ожидания при проверке статуса авторизации")
            except Exception as e:
                print(f"Ошибка при проверке авторизации: {str(e)}")
                raise Exception(f"Ошибка проверки авторизации: {str(e)}")
            
            if is_authorized:
                print("Пользователь уже авторизован")
                self.status_label.setText("Вы уже авторизованы!")
                QTimer.singleShot(1000, self.accept)  # Закрываем диалог через 1 секунду
                return
            
            # Отправляем запрос на код
            print(f"Отправка кода на номер: {self.phone}")
            self.status_label.setText(f"Отправка кода на номер {self.phone}...")
            try:
                code_request_timeout = 30  # 30 секунд таймаут
                print(f"Ожидание отправки кода (таймаут {code_request_timeout} сек)")
                await asyncio.wait_for(self.client.send_code_request(self.phone), code_request_timeout)
                print("Код успешно отправлен")
            except asyncio.TimeoutError:
                print("Превышено время ожидания при отправке кода")
                raise TimeoutError("Превышено время ожидания при отправке кода. Возможно проблемы с API Telegram или вашим интернет-соединением.")
            
            # Обновляем интерфейс
            self.status_label.setText("Код отправлен на ваш номер")
            self.code_input.setEnabled(True)
            self.verify_code_btn.setEnabled(True)
            self.send_code_btn.setEnabled(False)
            self.phone_input.setEnabled(False)
            
        except asyncio.TimeoutError:
            error_message = "Превышено время ожидания ответа от Telegram API"
            print(error_message)
            QMessageBox.critical(self, "Ошибка", error_message)
            self.status_label.setText(f"Ошибка: {error_message}")
            # Повторно включаем поле ввода и кнопку в случае ошибки
            self.phone_input.setEnabled(True)
            self.send_code_btn.setEnabled(True)
            # Отклоняем диалог в случае критической ошибки
            self.reject()
            
        except Exception as e:
            error_message = str(e)
            print(f"Подробная ошибка при отправке кода: {error_message}")
            QMessageBox.critical(self, "Ошибка", f"Ошибка при отправке кода: {error_message}")
            self.status_label.setText(f"Ошибка: {error_message}")
            
            # Повторно включаем поле ввода и кнопку в случае ошибки
            self.phone_input.setEnabled(True)
            self.send_code_btn.setEnabled(True)
            # Отклоняем диалог в случае критической ошибки
            self.reject()
    
    async def _async_verify_code(self):
        """Асинхронная проверка кода"""
        try:
            print("Запуск _async_verify_code")
            # Создаем таймер для контроля выполнения
            verify_code_timer = QTimer()
            verify_code_timer.setSingleShot(True)
            verify_code_timer.timeout.connect(lambda: self._handle_timeout("проверки кода"))
            verify_code_timer.start(45000)  # 45 секунд таймаута (увеличенное время)
            
            # Запускаем проверку кода
            print("Вызов функции verify_code")
            await self.verify_code()
            
            # Если мы дошли до этой точки, останавливаем таймер
            print("Функция verify_code завершилась успешно, останавливаем таймер")
            verify_code_timer.stop()
        except Exception as e:
            print(f"Ошибка при проверке кода в _async_verify_code: {str(e)}")
            self.status_label.setText(f"Ошибка: {str(e)}")
            # Повторно включаем поле ввода и кнопку в случае ошибки
            self.code_input.setEnabled(True)
            self.verify_code_btn.setEnabled(True)

    async def verify_code(self):
        """Verify the authentication code"""
        print("Функция verify_code вызвана")
        if not self.client:
            error_message = "Сначала отправьте запрос на код"
            self.status_label.setText(error_message)
            print(error_message)
            QMessageBox.warning(self, "Ошибка", error_message)
            return
        
        code = self.code_input.text().strip()
        if not code:
            error_message = "Введите код подтверждения"
            print(error_message)
            QMessageBox.warning(self, "Ошибка", error_message)
            return
        
        try:
            self.status_label.setText("Проверка кода...")
            print(f"Проверка кода: {code}")
            
            # Проверяем подключение
            if not self.client.is_connected():
                print("Клиент не подключен, повторное подключение")
                self.status_label.setText("Восстановление подключения к Telegram...")
                try:
                    connect_timeout = 15  # 15 секунд таймаут
                    print(f"Ожидание подключения (таймаут {connect_timeout} сек)")
                    await asyncio.wait_for(self.client.connect(), connect_timeout)
                    print("Подключение восстановлено успешно")
                except asyncio.TimeoutError:
                    print("Превышено время ожидания при подключении")
                    raise TimeoutError("Не удалось подключиться к серверам Telegram. Проверьте ваше интернет-соединение.")
                except Exception as e:
                    print(f"Ошибка при восстановлении подключения: {str(e)}")
                    raise Exception(f"Ошибка при восстановлении подключения: {str(e)}")
            
            # Отправляем код для входа
            print("Отправка кода для входа")
            self.status_label.setText("Проверка кода авторизации...")
            try:
                signin_timeout = 30  # 30 секунд таймаут
                print(f"Ожидание проверки кода (таймаут {signin_timeout} сек)")
                await asyncio.wait_for(self.client.sign_in(self.phone, code), signin_timeout)
                print("Код принят, авторизация успешна")
            except asyncio.TimeoutError:
                print("Превышено время ожидания при проверке кода")
                raise TimeoutError("Превышено время ожидания при проверке кода. Возможно проблемы с API Telegram или вашим интернет-соединением.")
            
            # Проверяем, что авторизация действительно прошла успешно
            print("Проверка успешности авторизации")
            try:
                auth_check_timeout = 10  # 10 секунд таймаут
                is_authorized = await asyncio.wait_for(self.client.is_user_authorized(), auth_check_timeout)
                if not is_authorized:
                    print("Авторизация не удалась несмотря на принятие кода")
                    raise Exception("Авторизация не удалась. Попробуйте перезапустить приложение.")
                print(f"Статус авторизации: {is_authorized}")
            except asyncio.TimeoutError:
                print("Превышено время ожидания при проверке успешности авторизации")
                # Предполагаем, что авторизация всё же прошла успешно
            
            # Обновляем интерфейс
            self.status_label.setText("Успешная авторизация")
            print("Диалог авторизации будет закрыт через 1 секунду")
            QTimer.singleShot(1000, self.accept)
            
        except asyncio.TimeoutError:
            error_message = "Превышено время ожидания ответа от Telegram API"
            print(error_message)
            QMessageBox.critical(self, "Ошибка", error_message)
            self.status_label.setText(f"Ошибка: {error_message}")
            # Разблокируем кнопку проверки кода
            self.verify_code_btn.setEnabled(True)
            
        except PhoneCodeInvalidError:
            print("Неверный код")
            QMessageBox.warning(self, "Ошибка", "Неверный код")
            self.status_label.setText("Неверный код")
            self.code_input.clear()
            self.verify_code_btn.setEnabled(True)
            
        except SessionPasswordNeededError:
            print("Требуется двухфакторная аутентификация — запрашиваем пароль")
            self.status_label.setText("Требуется пароль двухфакторной аутентификации")
            password, ok = QInputDialog.getText(
                self, "Двухфакторная аутентификация",
                "Введите пароль (облачный пароль Telegram):",
                QLineEdit.Password,
            )
            if not ok or not password:
                self.status_label.setText("Ввод пароля 2FA отменён")
                self.verify_code_btn.setEnabled(True)
                return
            try:
                await asyncio.wait_for(self.client.sign_in(password=password), 30)
                self.status_label.setText("Успешная авторизация (2FA)")
                print("2FA-пароль принят")
                QTimer.singleShot(1000, self.accept)
            except Exception as e:
                print(f"Ошибка 2FA: {e}")
                QMessageBox.critical(self, "Ошибка", f"Неверный пароль 2FA: {e}")
                self.status_label.setText("Неверный пароль 2FA")
                self.verify_code_btn.setEnabled(True)
            
        except Exception as e:
            error_message = str(e)
            print(f"Подробная ошибка при проверке кода: {error_message}")
            QMessageBox.critical(self, "Ошибка", f"Ошибка при проверке кода: {error_message}")
            self.status_label.setText(f"Ошибка: {error_message}")
            
            # Повторно включаем кнопку проверки кода в случае ошибки
            self.verify_code_btn.setEnabled(True)

    def get_client(self):
        """Return the authenticated client"""
        return self.client

    def accept(self):
        """Переопределяем метод accept"""
        print("Диалог принят")
        # Отменяем все асинхронные задачи перед закрытием
        if self.check_session_task and not self.check_session_task.done():
            print("Отмена задачи проверки сессии")
            self.check_session_task.cancel()
        
        # Создаем и показываем основное окно
        if self.client:
            print("Создание основного окна")
            self.main_window = NewsAnalyzerApp(self.client)  # Сохраняем ссылку на основное окно
            self.main_window.show()
            self.main_window.run_self_check_async()
            print("Основное окно отображено")
            # Используем QTimer для небольшой задержки перед закрытием диалога
            QTimer.singleShot(500, lambda: self._complete_accept())
        else:
            print("Ошибка: клиент не инициализирован")
            super().reject()
            QApplication.quit()
    
    def _complete_accept(self):
        """Завершение процесса принятия диалога"""
        print("Завершение процесса принятия диалога")
        try:
            super().accept()
        except Exception as e:
            print(f"Ошибка при закрытии диалога: {str(e)}")
            QApplication.quit()

    def reject(self):
        """Переопределяем метод reject"""
        print("Диалог отклонен")
        # Отменяем все асинхронные задачи перед закрытием
        if self.check_session_task and not self.check_session_task.done():
            print("Отмена задачи проверки сессии")
            self.check_session_task.cancel()
        
        super().reject()
        # Закрываем приложение
        QTimer.singleShot(100, QApplication.quit)  # Добавляем небольшую задержку перед закрытием

    def closeEvent(self, event):
        """Обработка закрытия окна"""
        print("Закрытие диалога")
        # Отменяем все асинхронные задачи перед закрытием
        if self.check_session_task and not self.check_session_task.done():
            print("Отмена задачи проверки сессии")
            self.check_session_task.cancel()
        
        # Добавляем задержку перед закрытием приложения
        QTimer.singleShot(100, QApplication.quit)
        event.accept()


class QRAuthDialog(QDialog):
    """Авторизация через QR-код: пользователь сканирует код в Telegram.

    Не нужен SMS/код подтверждения — обходит проблему недоставки кода.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.client = None
        self.main_window = None
        self._authorized = False
        self.setWindowTitle("Вход в Telegram — QR-код")
        self.setFixedSize(440, 560)
        layout = QVBoxLayout()

        title = QLabel("Вход через QR-код")
        title.setAlignment(Qt.AlignCenter)
        f = title.font(); f.setPointSize(16); f.setBold(True); title.setFont(f)
        layout.addWidget(title)

        steps = QLabel(
            "1. Откройте Telegram на телефоне\n"
            "2. Настройки → Устройства → Подключить устройство\n"
            "3. Наведите камеру на QR-код ниже"
        )
        steps.setAlignment(Qt.AlignCenter)
        layout.addWidget(steps)

        self.qr_label = QLabel()
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setFixedSize(300, 300)
        layout.addWidget(self.qr_label, alignment=Qt.AlignCenter)

        self.status_label = QLabel("Подключение к Telegram…")
        self.status_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status_label)

        self.setLayout(layout)
        asyncio.ensure_future(self._run())

    def _render_qr(self, url):
        matrix = qr_matrix(url)
        n = len(matrix)
        scale = max(4, 290 // n)
        size = n * scale
        pix = QPixmap(size, size)
        pix.fill(Qt.white)
        p = QPainter(pix)
        p.setPen(Qt.NoPen)
        p.setBrush(Qt.black)
        for y, row in enumerate(matrix):
            for x, val in enumerate(row):
                if val:
                    p.drawRect(x * scale, y * scale, scale, scale)
        p.end()
        self.qr_label.setPixmap(pix)

    async def _run(self):
        try:
            self.client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
            await self.client.connect()
            if await self.client.is_user_authorized():
                self._success()
                return
            self.status_label.setText("Отсканируйте QR-код в приложении Telegram")
            while not self._authorized:
                qr = await self.client.qr_login()
                self._render_qr(qr.url)
                try:
                    await qr.wait(timeout=30)
                    self._authorized = True
                except asyncio.TimeoutError:
                    self.status_label.setText("QR обновлён — отсканируйте заново")
                    continue
                except SessionPasswordNeededError:
                    password, ok = QInputDialog.getText(
                        self, "Двухфакторная аутентификация",
                        "Введите облачный пароль Telegram:", QLineEdit.Password
                    )
                    if not ok or not password:
                        self.status_label.setText("Вход отменён")
                        return
                    await self.client.sign_in(password=password)
                    self._authorized = True
            self._success()
        except Exception as e:
            print(f"Ошибка QR-авторизации: {e}")
            self.status_label.setText(f"Ошибка: {str(e)[:80]}")

    def _success(self):
        print("QR-авторизация успешна")
        self.status_label.setText("Успешный вход!")
        self.main_window = NewsAnalyzerApp(self.client)
        self.main_window.show()
        self.main_window.run_self_check_async()
        QTimer.singleShot(600, self.accept)

    def closeEvent(self, event):

        if not self._authorized:
            QTimer.singleShot(100, QApplication.quit)
        event.accept()


class NewsAnalyzerApp(QMainWindow):
    progress_signal = pyqtSignal(int)
    
    def __init__(self, telegram_client):
        super().__init__()
        self.setWindowTitle("News Analyzer")
        self.setGeometry(100, 100, 1000, 600)
        self.news_data = []  # Will store data from Word file
        self.results = []  # Will store analysis results
        self.client = telegram_client
        # Добавляем семафор для ограничения одновременных запросов к Telegram API
        self.telegram_semaphore = asyncio.Semaphore(1)  # Только 1 операция с Telegram API одновременно
        # Кэш постов каналов: {(channel_id, date): [posts]}
        self.posts_cache = {}
        self.initUI()
        self.progress_signal.connect(self.update_progress)

    def initUI(self):
        layout = QVBoxLayout()
        
        # Load Word file button
        self.load_button = QPushButton("Загрузить Word-файл")
        self.load_button.clicked.connect(self.on_load_clicked)
        layout.addWidget(self.load_button)
        
        # Удаляем выбор дат
        # Вместо этого просто добавим информационную метку
        self.info_label = QLabel("Анализ будет проведен только за дату целевой статьи. Время публикации указано в GMT+5.")
        layout.addWidget(self.info_label)
        
        # Analysis button
        self.analyze_button = QPushButton("Запустить анализ")
        self.analyze_button.clicked.connect(self.on_analyze_clicked)
        layout.addWidget(self.analyze_button)
        
        # Export button
        self.export_button = QPushButton("Экспорт в Excel")
        self.export_button.clicked.connect(self.export_to_excel)
        self.export_button.setEnabled(False)  # Disabled until analysis is done
        layout.addWidget(self.export_button)
        
        # Progress bar
        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)
        
        # Results table
        self.table = QTableWidget()
        self.table.setColumnCount(11)  # Увеличиваем количество столбцов с 6 до 11
        self.table.setHorizontalHeaderLabels([
            "Название статьи", "Zakon", "Zakon время", "Nur", "Nur время", 
            "Atameken", "Atameken время", "ZTB", "ZTB время", "Sputnik", "Sputnik время"
        ])
        
        # Set table to stretch to fill the available space
        header = self.table.horizontalHeader()
        for i in range(11):  # Обновляем количество столбцов здесь тоже
            header.setSectionResizeMode(i, QHeaderView.Stretch)
            
        layout.addWidget(self.table)
        
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)
        
        # Status label
        self.status_label = QLabel("Загрузите файл .docx для начала работы")
        layout.addWidget(self.status_label)

    @asyncSlot()
    def on_load_clicked(self):
        """Асинхронный обработчик нажатия кнопки загрузки файла"""
        print("Кнопка 'Загрузить Word-файл' нажата")
        self.status_label.setText("Подготовка к загрузке файла...")
        return self.load_word_file()  # Возвращаем корутину напрямую

    def parse_docx(self, file_path):
        """Extract all Telegram links from a .docx file.

        Поддерживает оба формата: ссылка на отдельной строке И ссылка
        внутри строки после заголовка ("Заголовок новости https://t.me/...").
        Дедуплицирует и дополняет ссылками из гиперссылок-relationships.
        """
        doc = Document(file_path)
        links = []
        seen = set()
        
        print(f"Анализирую документ: {file_path}")
        
        # Просматриваем все абзацы для поиска ссылок
        for paragraph in doc.paragraphs:
            for url in find_telegram_links(paragraph.text):
                if url not in seen:
                    seen.add(url)
                    links.append(url)
                    print(f"Найдена ссылка: {url}")
        
        
        for rel_id, rel in doc.part.rels.items():
            if "hyperlink" in rel.reltype and "t.me/" in rel._target:
                url = rel._target
                if url not in seen:
                    seen.add(url)
                    links.append(url)
                    print(f"Найдена гиперссылка: {url}")
                    print(f"Найдена гиперссылка: {rel._target}")
        
        print(f"Всего найдено {len(links)} ссылок в документе")
        return links

    async def ensure_connected(self):
        """Проверить подключение к Telegram и переподключиться при необходимости"""
        if not self.client.is_connected():
            print("Клиент отключён, переподключаемся...")
            await asyncio.wait_for(self.client.connect(), timeout=15.0)
            print("Переподключение успешно")

    async def get_title_from_telegram(self, link):
        """Get post title from Telegram using link"""
        # Используем семафор для ограничения доступа к Telegram API
        async with self.telegram_semaphore:
            try:
                message_id = self.extract_message_id_from_link(link)
                if not message_id:
                    return "Ссылка не распознана"

                channel_name = extract_channel_name(link)
                if not channel_name:
                    return "Неверный формат ссылки"

                print(f"Запрос сообщения из канала {channel_name}, ID: {message_id}")

                try:
                    # Проверяем подключение перед запросом
                    await self.ensure_connected()

                    message = await asyncio.wait_for(
                        self.client.get_messages(channel_name, ids=message_id),
                        timeout=45.0
                    )

                    if not message:
                        return "Сообщение не найдено"

                    if not message.text:
                        return "Текст сообщения отсутствует"

                    lines = message.text.strip().split('\n')
                    title = lines[0]

                    if len(title) > 100:
                        title = title[:97] + "..."

                    print(f"Успешно получен заголовок для {link}")
                    return title
                except asyncio.TimeoutError:
                    print(f"Тайм-аут при получении сообщения {message_id} из {channel_name}")
                    # Пробуем переподключиться и повторить один раз
                    try:
                        print("Попытка переподключения и повторного запроса...")
                        await self.client.disconnect()
                        await asyncio.wait_for(self.client.connect(), timeout=15.0)
                        message = await asyncio.wait_for(
                            self.client.get_messages(channel_name, ids=message_id),
                            timeout=45.0
                        )
                        if message and message.text:
                            lines = message.text.strip().split('\n')
                            title = lines[0]
                            if len(title) > 100:
                                title = title[:97] + "..."
                            print(f"Успешно получен заголовок после переподключения")
                            return title
                    except Exception as retry_e:
                        print(f"Повторная попытка тоже не удалась: {retry_e}")
                    return "Тайм-аут получения сообщения"

            except Exception as e:
                print(f"Ошибка при получении заголовка по ссылке {link}: {e}")
                return f"Ошибка: {str(e)[:50]}"
            
    async def load_word_file(self):
        """Load .docx file containing news links and get titles from Telegram"""
        file_name, _ = QFileDialog.getOpenFileName(
            self, "Выбрать Word-файл", "", "Word Files (*.docx)"
        )
        
        if file_name:
            try:
                # Очищаем предыдущие данные
                self.table.clearContents()
                self.table.setRowCount(0)
                self.progress_bar.setValue(0)
                
                print(f"Загрузка файла: {file_name}")
                self.status_label.setText("Загрузка ссылок из файла...")
                self.news_data = self.parse_docx(file_name)
                print(f"Результат парсинга: {len(self.news_data)} ссылок")
                
                if not self.news_data:
                    self.status_label.setText("Файл пуст или не содержит ссылок на Telegram")
                    return
                
                # Проверяем подключение к Telegram API перед продолжением
                if not self.client.is_connected():
                    print("Клиент Telegram не подключен. Попытка переподключения...")
                    self.status_label.setText("Переподключение к Telegram API перед загрузкой заголовков...")
                    try:
                        await asyncio.wait_for(self.client.connect(), timeout=10.0)
                        print("Переподключение успешно")
                    except (asyncio.TimeoutError, Exception) as e:
                        error_message = f"Не удалось подключиться к Telegram API: {str(e)}"
                        print(error_message)
                        self.status_label.setText(error_message)
                        QMessageBox.critical(self, "Ошибка подключения", error_message)
                        return
                    
                # Отображаем таблицу с подготовкой для загрузки заголовков
                self.table.setRowCount(len(self.news_data))
                self.progress_bar.setValue(0)
                
                # Временно заполняем таблицу только ссылками
                for row, link in enumerate(self.news_data):
                    # Заголовок пока неизвестен
                    self.table.setItem(row, 0, QTableWidgetItem("Загрузка заголовка..."))
                    # Ссылка на zakon
                    self.table.setItem(row, 1, QTableWidgetItem(link))
                    # Ячейка для времени Zakon (пока пустая)
                    self.table.setItem(row, 2, QTableWidgetItem(""))
                    
                    # Инициализируем остальные ячейки пустыми значениями
                    for col in range(3, self.table.columnCount()):
                        self.table.setItem(row, col, QTableWidgetItem(""))
                
                # Даем основному потоку возможность обновить интерфейс
                await asyncio.sleep(0.2)
                
                # Асинхронно загружаем заголовки для всех ссылок
                self.status_label.setText("Получение заголовков из Telegram...")
                # Используем конструкцию try-except для обработки возможных ошибок при загрузке заголовков
                try:
                    await self.load_titles_for_links()
                except Exception as e:
                    self.status_label.setText(f"Ошибка при загрузке заголовков: {str(e)}")
                    print(f"Ошибка при загрузке заголовков: {str(e)}")
                    traceback.print_exc()
                
            except Exception as e:
                self.status_label.setText(f"Ошибка при загрузке файла: {str(e)}")
                print(f"Подробная ошибка при загрузке файла: {str(e)}")
                traceback.print_exc()
    
    async def load_titles_for_links(self):
        """Asynchronously load titles for all links in the table"""
        try:
            total_links = len(self.news_data)
            self.status_label.setText(f"Получение заголовков для {total_links} ссылок...")
            print(f"Начинаю загрузку заголовков для {total_links} ссылок...")
            
            for row, link in enumerate(self.news_data):
                try:
                    print(f"Загрузка заголовка {row+1}/{total_links}: {link}")
                    # Специально не используем здесь семафор, так как он уже используется внутри get_title_from_telegram
                    # Это обеспечивает последовательное выполнение запросов к Telegram API
                    title = await self.get_title_from_telegram(link)
                    self.table.setItem(row, 0, QTableWidgetItem(title))
                    print(f"Получен заголовок: {title}")
                    
                    # Увеличиваем задержку между запросами, чтобы предотвратить блокировку API
                    await asyncio.sleep(0.5)  # Увеличиваем с 0.1 до 0.5 секунды
                    
                    # Обновляем прогресс
                    progress_percent = int((row + 1) * 100 / total_links)
                    self.progress_signal.emit(progress_percent)
                    
                    if (row + 1) % 5 == 0 or (row + 1) == total_links:  # Уменьшаем частоту обновления статуса
                        status_message = f"Получено {row + 1}/{total_links} заголовков ({progress_percent}%)..."
                        self.status_label.setText(status_message)
                        print(status_message)
                except Exception as e:
                    print(f"Ошибка при загрузке заголовка для ссылки {link}: {str(e)}")
                    self.table.setItem(row, 0, QTableWidgetItem(f"Ошибка: {str(e)[:50]}..."))
                    # Продолжаем обработку других ссылок даже при ошибке
                    await asyncio.sleep(1.0)  # Делаем более длинную паузу после ошибки
            
            self.status_label.setText(f"Загружено {total_links} новостей из файла")
            print(f"Завершена загрузка заголовков. Всего обработано: {total_links}")
            
        except Exception as e:
            error_message = f"Ошибка при загрузке заголовков: {str(e)}"
            self.status_label.setText(error_message)
            print(error_message)
            traceback.print_exc()

    def update_progress(self, value):
        """Update the progress bar"""
        self.progress_bar.setValue(value)

    def extract_message_id_from_link(self, link):
        """Extract message ID from a Telegram link"""
        return extract_message_id(link)

    async def get_post_from_target_channel(self, message_id):
        """Get post from target channel using message ID (с реконнектом при сбое)"""
        try:
            message = await with_reconnect(
                self.client,
                lambda: self.client.get_messages(target_channel, ids=message_id),
            )
            if message and message.text:
                return {
                    'text': message.text,
                    'link': f"https://t.me/{target_channel}/{message_id}",
                    'date': message.date
                }
        except Exception as e:
            print(f"Ошибка при получении сообщения {message_id}: {str(e)}")
        return None

    async def fetch_channel_posts(self, channel, start_date, end_date):
        """Fetch posts from a channel within the specified date range.

        Использует offset_date для начала загрузки сразу с нужной даты,
        вместо перебора всех сообщений канала с самого последнего.
        """
        posts = []
        skipped_posts = 0
        total_posts = 0

        print(f"\n=== Загрузка сообщений из канала {channel} ===")
        print(f"Период: с {start_date} по {end_date}")

        try:
            await self.ensure_connected()
            # offset_date — начинаем загрузку с конца целевого дня (end_date + 1 день, 00:00 UTC)
            # Telethon вернёт сообщения ДО этой даты, от новых к старым
            offset_dt = datetime(end_date.year, end_date.month, end_date.day, tzinfo=KZ_TZ) + timedelta(days=1)

            message_gen = self.client.iter_messages(
                channel,
                offset_date=offset_dt,
                limit=None  # без лимита — выходим по дате
            )

            while True:
                try:
                    try:
                        message = await asyncio.wait_for(message_gen.__anext__(), timeout=15.0)
                    except StopAsyncIteration:
                        break

                    total_posts += 1

                    if total_posts % 100 == 0:
                        print(f"Обработано {total_posts} сообщений, найдено подходящих: {len(posts)}")

                    if not hasattr(message, 'date') or not message.date:
                        skipped_posts += 1
                        continue

                    message_date = kz_date(message.date)

                    # Сообщения идут от новых к старым — если ушли за start_date, выходим
                    if message_date < start_date:
                        print(f"Достигнута дата раньше начальной ({message_date} < {start_date}), прекращаем поиск")
                        break

                    # Проверка на вхождение в диапазон дат
                    if start_date <= message_date <= end_date:
                        if hasattr(message, 'text') and message.text:
                            posts.append({
                                'date': message.date,
                                'text': message.text,
                                'link': f"https://t.me/{channel}/{message.id}"
                            })
                        else:
                            skipped_posts += 1

                except asyncio.TimeoutError:
                    print(f"Тайм-аут при получении сообщения из канала {channel}, пропускаем и продолжаем")
                    continue
                except Exception as e:
                    print(f"Ошибка при обработке сообщения из канала {channel}: {str(e)}")
                    continue

            print(f"\nРезультаты загрузки из канала {channel}:")
            print(f"Всего обработано: {total_posts} сообщений")
            print(f"Найдено подходящих: {len(posts)} сообщений")
            print(f"Пропущено (без текста или без даты): {skipped_posts} сообщений")

            if len(posts) == 0:
                print(f"ВНИМАНИЕ: Не найдено ни одного подходящего сообщения в канале {channel} за указанный период!")
                print(f"Проверьте правильность выбранного диапазона дат и доступ к каналу.")

        except Exception as e:
            print(f"\nОШИБКА при получении сообщений из канала {channel}: {str(e)}")
            traceback.print_exc()

        return posts

    def _extract_keywords(self, doc):
        keywords = set()
        for token in doc:
            if token.pos_ in ("NOUN", "VERB", "ADJ", "PROPN") and not token.is_stop and len(token.text) > 2:
                keywords.add(token.lemma_.lower())
        return keywords

    def _extract_entities(self, doc):
        entities = set()
        for ent in doc.ents:
            if ent.label_ in ("PER", "ORG", "LOC", "GPE"):
                entities.add(ent.text.lower().strip())
        return entities

    def _safe_nlp(self, text):
        try:
            return nlp(text)
        except Exception:
            return nlp(text[:5000])

    async def compute_article_features(self, article_text):
        """Один раз посчитать эмбеддинг, ключевые слова и сущности статьи."""
        loop = asyncio.get_event_loop()
        processed = preprocess_text(article_text)[:10000]
        embedding = await loop.run_in_executor(
            None,
            lambda: sentence_model.encode(processed, convert_to_tensor=True, normalize_embeddings=True)
        )
        doc = await loop.run_in_executor(None, self._safe_nlp, processed)
        keywords = self._extract_keywords(doc)
        entities = self._extract_entities(doc)
        return {'processed': processed, 'embedding': embedding,
                'keywords': keywords, 'entities': entities}

    async def score_all_posts(self, article_features, article_date, channel_posts):
        """Score all channel posts against an article. Returns list of (post, details) sorted by combined score desc."""
        if not channel_posts:
            print("Нет постов для сравнения")
            return []

        try:
            article_embedding = article_features['embedding']
            article_keywords = article_features['keywords']
            article_entities = article_features['entities']

            # 2. Предобработка всех текстов постов (batch)
            valid_posts = []
            processed_post_texts = []
            for post in channel_posts:
                if not post['text'] or not post['text'].strip():
                    continue
                processed = preprocess_text(post['text'])
                if not processed:
                    continue
                if len(processed) > 10000:
                    processed = processed[:10000]
                valid_posts.append(post)
                processed_post_texts.append(processed)

            if not valid_posts:
                print("Нет валидных постов для сравнения после предобработки")
                return []

            print(f"Начинаем оценку постов (порог схожести: {similarity_threshold})")
            print(f"Валидных постов для сравнения: {len(valid_posts)}")


            loop = asyncio.get_event_loop()
            post_embeddings = await loop.run_in_executor(
                None,
                lambda: sentence_model.encode(processed_post_texts, batch_size=32,
                                              convert_to_tensor=True, normalize_embeddings=True)
            )

            # 4. Матричное вычисление cosine similarity
            cos_scores = util.cos_sim(article_embedding, post_embeddings)[0]

            print(f"Используем {len(article_keywords)} ключевых слов, {len(article_entities)} сущностей из статьи")
            if article_entities:
                print(f"Сущности: {', '.join(list(article_entities)[:15])}")


            def _run_pipe(texts):
                kws, ents = [], []
                for d in nlp.pipe(texts, batch_size=50):
                    kws.append(self._extract_keywords(d))
                    ents.append(self._extract_entities(d))
                return kws, ents

            try:
                all_post_keywords, all_post_entities = await loop.run_in_executor(
                    None, _run_pipe, processed_post_texts
                )
            except Exception as e:
                print(f"Ошибка batch spaCy, переключаемся на поштучную обработку: {e}")
                all_post_keywords = []
                all_post_entities = []
                for text in processed_post_texts:
                    try:
                        doc = nlp(text)
                        all_post_keywords.append(self._extract_keywords(doc))
                        all_post_entities.append(self._extract_entities(doc))
                    except Exception:
                        all_post_keywords.append(set())
                        all_post_entities.append(set())

            # 6. Оценка всех постов
            scored_posts = []

            for idx, (post, post_keywords, post_entities) in enumerate(zip(valid_posts, all_post_keywords, all_post_entities)):
                try:
                    vector_similarity = cos_scores[idx].item()

                    # NER overlap
                    if article_entities and post_entities:
                        shared_entities = article_entities & post_entities
                        ner_score = len(shared_entities) / max(len(article_entities), 1)
                    else:
                        shared_entities = set()
                        ner_score = 0

                    # Jaccard по ключевым словам
                    if article_keywords and post_keywords:
                        common_keywords = article_keywords & post_keywords
                        keywords_overlap = len(common_keywords) / len(article_keywords | post_keywords)
                    else:
                        common_keywords = set()
                        keywords_overlap = 0



                    time_diff = abs((article_date - post['date']).total_seconds()) / 3600
                    combined = combined_score(
                        vector=vector_similarity,
                        ner=ner_score,
                        keywords=keywords_overlap,
                        time_diff_hours=time_diff
                    )

                    if combined >= similarity_threshold:
                        details = {
                            'combined': combined,
                            'vector': vector_similarity,
                            'ner_score': ner_score,
                            'keywords': keywords_overlap,
                            'time_diff_hours': time_diff,
                            'shared_entities': shared_entities,
                            'common_keywords': common_keywords
                        }
                        scored_posts.append((post, details))

                except Exception as e:
                    print(f"Ошибка при оценке поста ({post.get('link', 'N/A')}): {str(e)}")
                    continue

            # Сортируем по убыванию combined score
            scored_posts.sort(key=lambda x: x[1]['combined'], reverse=True)

            if scored_posts:
                top = scored_posts[0][1]
                print(f"\nЛучший кандидат: combined={top['combined']:.4f} "
                      f"(vec={top['vector']:.4f}, ner={top['ner_score']:.4f}, kw={top['keywords']:.4f})")
                print(f"Ссылка: {scored_posts[0][0]['link']}")
                if top['shared_entities']:
                    print(f"Общие сущности: {', '.join(list(top['shared_entities'])[:10])}")
            else:
                print("\nНе найдено постов выше порога схожести")

            print(f"Всего кандидатов выше порога: {len(scored_posts)}")
            return scored_posts

        except Exception as e:
            print(f"Ошибка при оценке постов: {str(e)}")
            traceback.print_exc()
            return []

    @asyncSlot()
    def on_analyze_clicked(self):
        """Обработчик нажатия кнопки 'Запустить анализ'"""
        print("Кнопка 'Запустить анализ' нажата")
        self.status_label.setText("Подготовка к анализу...")
        
        # Проверяем состояние клиента Telegram перед запуском анализа
        if not self.client.is_connected():
            print("Клиент Telegram не подключен. Попытка переподключения...")
            self.status_label.setText("Попытка переподключения к Telegram API...")
            # Запускаем асинхронную задачу переподключения
            asyncio.create_task(self.reconnect_telegram_client())
            return
            
        # Возвращаем корутину анализа напрямую
        return self.start_analysis()

    async def reconnect_telegram_client(self):
        """Переподключение к клиенту Telegram"""
        try:
            print("Попытка переподключения к Telegram API...")
            self.status_label.setText("Переподключение к Telegram API...")
            
            # Пробуем переподключить клиент
            await self.client.connect()
            
            if await self.client.is_user_authorized():
                print("Переподключение успешно, запускаем анализ")
                self.status_label.setText("Переподключение успешно, запускаем анализ...")
                await self.start_analysis()
            else:
                print("Ошибка: сессия недействительна, требуется повторная авторизация")
                self.status_label.setText("Требуется повторная авторизация в Telegram")
                QMessageBox.critical(self, "Ошибка подключения", 
                                   "Не удалось авторизоваться в Telegram. Перезапустите приложение.")
        except Exception as e:
            error_message = f"Ошибка при переподключении к Telegram API: {str(e)}"
            print(error_message)
            self.status_label.setText(error_message)
            QMessageBox.critical(self, "Ошибка подключения", error_message)

    def run_self_check_async(self):
        """Запустить self-check доступа к каналам и показать итог в статусе."""
        asyncio.ensure_future(self._do_self_check())

    async def _do_self_check(self):
        try:
            check = await run_self_check(self.client, target_channel, list(channels.values()))
        except Exception as e:
            self.status_label.setText(f"Self-check не выполнен: {e}")
            return
        if not check["problems"]:
            self.status_label.setText("Проверка пройдена: связь и все каналы доступны")
        else:
            down = [n for n, ok in check["channels"].items() if not ok]
            msg = "Внимание: " + "; ".join(check["problems"][:4])
            self.status_label.setText(msg)
            if not check["target_ok"] or down:
                QMessageBox.warning(self, "Проверка доступа",
                                    "Есть проблемы с доступом:\n" + "\n".join(check["problems"]))

    async def start_analysis(self):
        """Start the analysis process with greedy deduplication per channel."""
        print("Начало анализа")
        if not self.news_data:
            self.status_label.setText("Сначала загрузите файл с новостями")
            return

        try:
            # Disable buttons during analysis
            self.load_button.setEnabled(False)
            self.analyze_button.setEnabled(False)
            self.status_label.setText("Анализ запущен...")


            self.status_label.setText("Проверка связи и доступа к каналам…")
            preflight = await run_self_check(self.client, target_channel, list(channels.values()))
            if not preflight['authorized']:
                self.status_label.setText("Нет авторизации в Telegram. Перезапустите приложение.")
                QMessageBox.critical(self, "Нет связи",
                                     "Не удалось подтвердить авторизацию в Telegram.\n"
                                     "Проверьте интернет и перезапустите приложение.")
                return
            if not preflight['target_ok']:
                self.status_label.setText(f"Нет доступа к каналу {target_channel}.")
                QMessageBox.critical(self, "Нет доступа",
                                     f"Не удалось получить доступ к каналу {target_channel}.\n"
                                     "Проверьте интернет-соединение и попробуйте снова.")
                return
            down_channels = [n for n, ok in preflight['channels'].items() if not ok]
            if down_channels:
                QMessageBox.warning(self, "Часть каналов недоступна",
                                    "Недоступны каналы: " + ", ".join(down_channels) +
                                    "\nАнализ продолжится по доступным каналам.")

            # Reset results
            self.results = []
            self.posts_cache = {}

            print(f"\n==== ЗАПУСК АНАЛИЗА (v2: NER + дедупликация) ====")
            print(f"Количество статей для анализа: {len(self.news_data)}")
            print(f"Порог схожести текстов: {similarity_threshold}")
            print(f"Каналы для сравнения: {', '.join(channels.keys())}")

            total_articles = len(self.news_data)
            found_articles = 0
            found_matches = {channel: 0 for channel in channels}

            # Фаза 1: Загрузка всех статей Zakon
            self.status_label.setText("Фаза 1: Загрузка статей Zakon...")
            articles_data = []  # [(row_idx, link, article_dict or None)]

            for row_idx, link in enumerate(self.news_data):
                message_id = self.extract_message_id_from_link(link)

                if not message_id:
                    print(f"[{row_idx}] Не удалось извлечь ID из ссылки: {link}")
                    self.table.setItem(row_idx, 1, QTableWidgetItem("Ссылка не распознана"))
                    articles_data.append((row_idx, link, None))
                    continue

                article = await self.get_post_from_target_channel(message_id)

                if not article:
                    print(f"[{row_idx}] Пост не найден по ID: {message_id}")
                    self.table.setItem(row_idx, 1, QTableWidgetItem("Пост не найден"))
                    articles_data.append((row_idx, link, None))
                    continue

                found_articles += 1
                article['features'] = await self.compute_article_features(article['text'])
                articles_data.append((row_idx, link, article))

                # Заполняем столбцы Zakon
                self.table.setItem(row_idx, 1, QTableWidgetItem(article['link']))
                self.table.setItem(row_idx, 2, QTableWidgetItem(to_kz(article['date']).strftime("%H:%M:%S")))

                progress_percent = int((row_idx + 1) * 50 / total_articles)
                self.progress_signal.emit(progress_percent)
                if (row_idx + 1) % 10 == 0:
                    self.status_label.setText(f"Загрузка статей Zakon: {row_idx + 1}/{total_articles}...")

            print(f"Загружено {found_articles} статей Zakon из {total_articles}")

            # Фаза 2: Для каждого канала — оценка всех пар и жадное назначение
            col_idx = 3
            for channel_name, channel_id in channels.items():
                print(f"\n==== Канал {channel_name} ({channel_id}) — оценка и назначение ====")
                self.status_label.setText(f"Анализ канала {channel_name}...")

                # Собираем все scores: [(row_idx, post, details), ...]
                all_candidates = []

                channel_error = None
                for art_idx, (row_idx, link, article) in enumerate(articles_data):
                    if article is None:
                        continue

                    target_date = kz_date(article['date'])

                    # Загружаем посты с кэшированием
                    cache_key = (channel_id, target_date)
                    if cache_key in self.posts_cache:
                        channel_posts = self.posts_cache[cache_key]
                    else:
                        try:
                            channel_posts = await self.fetch_channel_posts(channel_id, target_date, target_date)
                            self.posts_cache[cache_key] = channel_posts
                        except Exception as e:
                            print(f"Сбой загрузки {channel_id} за {target_date}: {e}")
                            channel_error = str(e)
                            channel_posts = []

                    # Получаем все scores для этой статьи
                    scored = await self.score_all_posts(article['features'], article['date'], channel_posts)

                    for post, details in scored:
                        all_candidates.append((row_idx, post, details))

                    if (art_idx + 1) % 20 == 0:
                        progress_percent = 50 + int((art_idx + 1) * 50 / (total_articles * len(channels)))
                        self.progress_signal.emit(min(progress_percent, 99))
                        self.status_label.setText(
                            f"Канал {channel_name}: оценено {art_idx + 1}/{total_articles} статей...")




                detail_by_key = {}
                score_candidates = []
                for row_idx, post, details in all_candidates:
                    detail_by_key[(row_idx, post['link'])] = (post, details)
                    score_candidates.append((row_idx, post['link'], details['combined']))

                assigned = greedy_assign(score_candidates)
                assignments = {row_idx: detail_by_key[(row_idx, link)] for row_idx, link in assigned.items()}

                print(f"\nКанал {channel_name}: назначено {len(assignments)} уникальных совпадений")

                # Заполняем таблицу
                for row_idx, link, article in articles_data:
                    if row_idx in assignments:
                        post, details = assignments[row_idx]
                        self.table.setItem(row_idx, col_idx, QTableWidgetItem(post['link']))
                        self.table.setItem(row_idx, col_idx + 1, QTableWidgetItem(to_kz(post['date']).strftime("%H:%M:%S")))
                        found_matches[channel_name] += 1
                        print(f"  [{row_idx}] -> {post['link']} (combined={details['combined']:.4f})")
                    else:
                        self.table.setItem(row_idx, col_idx, QTableWidgetItem("Не найдено"))
                        self.table.setItem(row_idx, col_idx + 1, QTableWidgetItem(""))

                if channel_error:
                    self.status_label.setText(
                        f"Канал {channel_name}: были проблемы со связью, результат может быть неполным")

                col_idx += 2

            # Финальная статистика
            print("\n==== РЕЗУЛЬТАТЫ АНАЛИЗА (v2) ====")
            print(f"Всего статей обработано: {total_articles}")
            print(f"Найдено исходных статей: {found_articles} из {total_articles}")

            matches_summary = []
            for channel_name in channels:
                match_percent = int(found_matches[channel_name] / total_articles * 100) if total_articles > 0 else 0
                matches_summary.append(f"{channel_name}: {found_matches[channel_name]} ({match_percent}%)")
                print(f"Совпадений в {channel_name}: {found_matches[channel_name]} из {total_articles} ({match_percent}%)")

            self.export_button.setEnabled(True)

            summary_message = f"Анализ завершен. Найдено совпадений: " + ", ".join(matches_summary)
            self.status_label.setText(summary_message)
            print(f"\n{summary_message}")
        
        except Exception as e:
            error_message = str(e)
            print(f"Подробная ошибка при анализе: {error_message}")
            self.status_label.setText(f"Ошибка при анализе: {error_message}")
            # Показываем диалог с ошибкой
            QMessageBox.critical(self, "Ошибка", f"Произошла ошибка при анализе:\n{error_message}")
            # Добавляем полный стек вызовов для отладки
            traceback.print_exc()
        
        finally:
            # Re-enable buttons
            self.load_button.setEnabled(True)
            self.analyze_button.setEnabled(True)
            self.progress_signal.emit(100)
            print("\nАнализ завершен")
            
        return len(found_matches)

    def export_to_excel(self):
        """Export results to Excel file"""
        if self.table.rowCount() == 0:
            self.status_label.setText("Нет данных для экспорта")
            return
            
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить в Excel", "", "Excel Files (*.xlsx)"
        )
        
        if file_path:
            if not file_path.endswith('.xlsx'):
                file_path += '.xlsx'
            try:
                # Create a dataframe from the table data
                data = []
                headers = [self.table.horizontalHeaderItem(i).text() 
                           for i in range(self.table.columnCount())]
                
                for row in range(self.table.rowCount()):
                    row_data = []
                    for col in range(self.table.columnCount()):
                        item = self.table.item(row, col)
                        if item:
                            row_data.append(item.text())
                        else:
                            row_data.append("")
                    data.append(row_data)
                
                df = pd.DataFrame(data, columns=headers)
                df.to_excel(file_path, index=False)
                self.status_label.setText(f"Данные успешно экспортированы в {file_path}")
                
            except Exception as e:
                self.status_label.setText(f"Ошибка при экспорте: {str(e)}")

async def main(app):
    """Main application entry point (qasync canonical pattern)."""
    print("Запуск приложения")
    app_close_event = asyncio.Event()
    app.aboutToQuit.connect(app_close_event.set)

    global nlp, sentence_model


    loading = QDialog()
    loading.setWindowTitle("News Analyzer — загрузка")

    loading.setWindowFlags(Qt.Window | Qt.CustomizeWindowHint | Qt.WindowTitleHint)
    loading.setFixedSize(480, 150)
    _loading_layout = QVBoxLayout(loading)
    _loading_label = QLabel("Загрузка моделей…")
    _loading_label.setAlignment(Qt.AlignCenter)
    _loading_layout.addWidget(_loading_label)
    _loading_bar = QProgressBar()
    _loading_bar.setRange(0, 100)
    _loading_bar.setValue(0)
    _loading_layout.addWidget(_loading_bar)
    _loading_sub = QLabel("Подготовка…")
    _loading_sub.setAlignment(Qt.AlignCenter)
    _loading_layout.addWidget(_loading_sub)
    loading.show()
    app.processEvents()

    loop = asyncio.get_event_loop()

    total_bytes = await loop.run_in_executor(None, bge_total_bytes)
    cache_dir = bge_cache_folder()

    def _update_loading_progress():
        done = dir_size_bytes(cache_dir)
        mb_done = done / (1024 * 1024)
        mb_total = total_bytes / (1024 * 1024)
        if total_bytes and done >= total_bytes * 0.999:
            _loading_bar.setValue(99)
            _loading_sub.setText(f"Почти готово, загрузка в память… ({mb_done:.0f} МБ)")
        else:
            pct = min(99, int(done * 100 / total_bytes)) if total_bytes else 0
            _loading_bar.setValue(pct)
            _loading_sub.setText(f"Скачано {mb_done:.0f} из ~{mb_total:.0f} МБ")

    _loading_timer = QTimer()
    _loading_timer.timeout.connect(_update_loading_progress)
    _loading_timer.start(500)
    _update_loading_progress()

    try:
        nlp, sentence_model = await loop.run_in_executor(None, load_models)
    except Exception as e:
        _loading_timer.stop()
        loading.close()
        box = QMessageBox(None)
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("Ошибка загрузки моделей")
        box.setText(f"Не удалось загрузить модели:\n{e}\n\n"
                    "Проверьте интернет-соединение и перезапустите приложение.")
        save_btn = box.addButton("Сохранить отчёт…", QMessageBox.ActionRole)
        box.addButton(QMessageBox.Close)
        box.exec_()
        if box.clickedButton() is save_btn:
            import diagnostics
            path, _ = QFileDialog.getSaveFileName(None, "Сохранить отчёт", "news-analyzer-report.txt")
            if path:
                diagnostics.save_report(path)
        app.quit()
        return
    _loading_timer.stop()
    _loading_bar.setValue(100)
    loading.close()

    print("Создание диалога авторизации")
    auth_dialog = QRAuthDialog()
    auth_dialog.show()

    await app_close_event.wait()

def main_test():
    """Тестовая функция для проверки сопоставления заголовков и ссылок"""
    app = QApplication(sys.argv)
    window = QMainWindow()
    window.setWindowTitle("Тест сопоставления")
    window.setGeometry(100, 100, 800, 600)
    
    # Создаем центральный виджет
    central_widget = QWidget()
    layout = QVBoxLayout(central_widget)
    
    # Создаем кнопку для загрузки файла
    load_button = QPushButton("Загрузить Word-файл")
    layout.addWidget(load_button)
    
    # Создаем таблицу для отображения результатов
    table = QTableWidget()
    table.setColumnCount(2)
    table.setHorizontalHeaderLabels(["Заголовок", "Ссылка"])
    header = table.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.Stretch)
    header.setSectionResizeMode(1, QHeaderView.Stretch)
    layout.addWidget(table)
    
    # Создаем метку статуса
    status_label = QLabel("Загрузите файл .docx")
    layout.addWidget(status_label)
    
    window.setCentralWidget(central_widget)
    
    # Класс для обработки файла
    class FileProcessor:
        def parse_docx(self, file_path):
            """Extract only Telegram links from a .docx file"""
            doc = Document(file_path)
            links = []
            
            print(f"Анализирую документ: {file_path}")
            
            # Просматриваем все абзацы для поиска ссылок
            for paragraph in doc.paragraphs:
                text = paragraph.text.strip()
                if text.startswith("https://t.me"):
                    links.append(text)
                    print(f"Найдена ссылка: {text}")
            
            # Если основной метод не нашел ссылок, извлекаем гиперссылки напрямую
            if not links:
                print("Прямой метод не нашел ссылок, извлекаем гиперссылки из документа")
                for rel_id, rel in doc.part.rels.items():
                    if "hyperlink" in rel.reltype and "t.me/" in rel._target:
                        links.append(rel._target)
                        print(f"Найдена гиперссылка: {rel._target}")
            
            print(f"Всего найдено {len(links)} ссылок в документе")
            return links
    
    processor = FileProcessor()
    
    # Обработчик нажатия кнопки загрузки файла
    def load_file():
        file_name, _ = QFileDialog.getOpenFileName(
            window, "Выбрать Word-файл", "", "Word Files (*.docx)"
        )
        
        if file_name:
            try:
                # Парсим файл
                data = processor.parse_docx(file_name)
                
                # Обновляем таблицу
                table.setRowCount(len(data))
                for row, link in enumerate(data):
                    title_item = QTableWidgetItem(link)
                    link_item = QTableWidgetItem(link)
                    table.setItem(row, 0, title_item)
                    table.setItem(row, 1, link_item)
                
                status_label.setText(f"Загружено {len(data)} ссылок")
                
            except Exception as e:
                status_label.setText(f"Ошибка при загрузке файла: {str(e)}")
                print(f"Ошибка: {str(e)}")
    
    # Связываем кнопку с обработчиком
    load_button.clicked.connect(load_file)
    
    # Показываем окно и запускаем приложение
    window.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "--test":
            main_test()
        else:
            import diagnostics
            diagnostics.enable_file_logging()
            print("Запуск main() через QEventLoop loop_factory")
            app = QApplication(sys.argv)
            app.setApplicationName("News Analyzer")
            app.setApplicationDisplayName("News Analyzer")
            _icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon_1024.png")
            if os.path.exists(_icon_path):
                app.setWindowIcon(QIcon(_icon_path))
            asyncio.run(main(app), loop_factory=lambda: QEventLoop(app))
    except Exception as e:
        print(f"Критическая ошибка при запуске приложения: {str(e)}")
        traceback.print_exc() 
