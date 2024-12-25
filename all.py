import json
from dataclasses import dataclass
from typing import Dict, TypeAlias
import sqlite3
import logging
from colorama import Fore, Style, init
import requests
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, types
import os
import random
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
import logging
import asyncio
from aiogram.types import ChatMemberUpdated
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter
from datetime import datetime
from peewee import *
from dotenv import load_dotenv

init(autoreset=True)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s", datefmt="%H:%M:%S")

API_TOKEN =  str(os.getenv("TOKEN"))
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

kwork_db = SqliteDatabase('kwork_projects.db')
kwork_db.connect()

habr_db = SqliteDatabase('habr_work.db')
habr_db.connect()

ADMIN = int(os.getenv("CHAT_ID"))

class BaseModel(Model):
    class Meta:
        database = kwork_db

class KworkProject(BaseModel):
    id = IntegerField(primary_key=True)
    link = CharField()
    title = CharField()
    description = TextField()
    price = IntegerField()

kwork_db.create_tables([KworkProject])

class HabrBaseModel(Model):
    class Meta:
        database = habr_db

class HabrArticle(HabrBaseModel):
    title = CharField()
    link = CharField(unique=True)
    price = CharField()
    sent = BooleanField(default=False)

class User(HabrBaseModel):  
    user_id = CharField(unique=True)

habr_db.create_tables([HabrArticle, User])  

@dataclass
class Kwork:
    title: str
    description: str
    price: int

Kworks: TypeAlias = Dict[int, Kwork]
KWORK_URL = 'https://kwork.ru/project/{}&page={}'
PROJECTS_URL = 'https://kwork.ru/projects'

def parse_kwork(id: int) -> Kwork:
    response = requests.get(KWORK_URL.format(id))
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")
    script_soup: BeautifulSoup = soup.find_all("script", type="application/ld+json")[-1]

    if not script_soup.string:
        raise Exception("No JSON data found")

    data = json.loads(script_soup.string.replace("\n", "\\r\\n"))

    return Kwork(
        data["name"],
        data["description"],
        int(float(data["offers"]["price"])),
    )

def get_kworks(category: int, page: int = 1) -> Kworks:
    response = requests.get(PROJECTS_URL, params={"c": category, "page": page})
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")

    if not soup.head:
        raise Exception("No head tag found")

    scripts = soup.head.find_all("script")
    js_script = ""
    for script in scripts:
        if script.text.startswith("window.ORIGIN_URL"):
            js_script = script.text
            break

    start_pointer = 0
    json_data = ""
    in_literal = False
    for current_pointer in range(len(js_script)):
        if js_script[current_pointer] == '"' and js_script[current_pointer - 1] != "\\":
            in_literal = not in_literal
            continue

        if in_literal or js_script[current_pointer] != ";":
            continue

        line = js_script[start_pointer:current_pointer].strip()
        if line.startswith("window.stateData"):
            json_data = line[17:]
            break

        start_pointer = current_pointer + 1

    data = json.loads(json_data)

    kworks: Kworks = dict()
    for raw_kwork in data["wantsListData"]["wants"]:
        kworks[raw_kwork["id"]] = Kwork(
            title=raw_kwork["name"],
            description=raw_kwork["description"],
            price=int(float(raw_kwork["priceLimit"])),
        )

    return kworks

async def save_to_database(kworks: Kworks):
    conn = sqlite3.connect("kwork_projects.db")
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY,
            link TEXT,
            title TEXT,
            description TEXT,
            price INTEGER
        )
    ''')

    for id, kwork in kworks.items():
        cursor.execute('SELECT id FROM projects WHERE id = ?', (id,))
        existing_record = cursor.fetchone()
        if existing_record:
            logging.info(f"{Fore.YELLOW}Кворк уже существует в базе: {id}{Style.RESET_ALL}")
            continue 

        link = f"https://kwork.ru/projects/{id}/view"
        cursor.execute('''
            INSERT INTO projects (id, link, title, description, price)
            VALUES (?, ?, ?, ?, ?)
        ''', (id, link, kwork.title, kwork.description, kwork.price))

        logging.info(
            f"{Fore.GREEN}Новый кворк: {link} | "
            f"Цена: {kwork.price} | "
            f"Название: {kwork.title} | "
            f"Описание: {kwork.description}{Style.RESET_ALL} | "
        )

        bot_start_menu = [
            [
                InlineKeyboardButton(text='Отклик', url=link),
            ]
        ]
        
        menu = InlineKeyboardMarkup(inline_keyboard=bot_start_menu)

        message = (
            f"Кворк-фриланс\n"
            f"Цена: {kwork.price}\n"
            f"Название: {kwork.title}\n"
            f"Описание: {kwork.description}"
        )
        try:
            await bot.send_message(chat_id=ADMIN, text=message, reply_markup=menu)
            await asyncio.sleep(2)  
        except Exception as e:
            logging.error(f"Ошибка при отправке сообщения: {e}")

    conn.commit()
    conn.close()

async def monitor_new_kworks(category: int, interval: int = 60, max_pages: int = 10):
    logging.info("Запуск мониторинга новых кворков...")
    while True:
        try:
            for page in range(1, max_pages + 1):
                logging.info(f"Парсинг страницы {page}...")
                new_kworks = get_kworks(category, page)
                await save_to_database(new_kworks)
        except Exception as e:
            logging.error(f"Ошибка при получении данных: {e}")

        logging.info(f"Ожидание следующей проверки через {interval} секунд...")
        await asyncio.sleep(interval)

async def parser(ioloop):
    url = 'https://freelance.habr.com/tasks?categories=development_backend%2Cdevelopment_bots%2Cdevelopment_other%2Cadmin%2Cdevelopment_frontend%2Cdevelopment_scripts%2Ctesting_sites%2Ccontent_specification%2Cmarketing_sales%2Cmarketing_research%2Cother_audit_analytics&page={}'

    max_pages = 50  

    while True:
        print("Request: ", datetime.now())
        articles_new = []

        for page in range(1, max_pages + 1):
            r = requests.get(url.format(page))
            soup = BeautifulSoup(r.text, "html.parser")
            articles = soup.findAll('article')

            for article in articles:
                title_element = article.find("div", class_="task__title")
                title_element_text = title_element.text
                link_element = title_element.find("a")
                link_element_href = link_element["href"]
                price_element_text = article.find("div", class_="task__price").text

                try:
                    art = HabrArticle.create(link=link_element_href,
                                             title=title_element_text,
                                             price=price_element_text,
                                             sent=False)
                    articles_new.append(art)
                except IntegrityError:
                    continue

        if len(articles_new) > 0:
            for new in articles_new:
                tg_str = (f'{new.title}\n'
                          f'Link: https://freelance.habr.com{new.link}\n'
                          f'Price: {new.price}\n\n')

                print(tg_str)
                itog = f'https://freelance.habr.com{new.link}'

                bot_start_menu = [
                    [
                        InlineKeyboardButton(text='Отклик', url=itog),
                    ]
                ]
                
                menu = InlineKeyboardMarkup(inline_keyboard=bot_start_menu)

                message = (
                    f"Хабр-фриланс\n"
                    f"Цена: {new.price}\n"
                    f"Название: {new.title}\n"
                )
                try:
                    await bot.send_message(chat_id=ADMIN, text=message, reply_markup=menu)
                    await asyncio.sleep(2)  
                except Exception as e:
                    logging.error(f"Ошибка при отправке сообщения: {e}")

                new.sent = True
                new.save()

        await asyncio.sleep(60)  
        
async def main():
    category = 41  
    logging.info("Бот запускается...")
    ioloop = asyncio.get_event_loop()

    USE_FREELANCE = int(os.getenv("USE_FREELANCE", 0))

    if USE_FREELANCE == 0:
        logging.info("Режим парсинга: ВЫКЛЮЧЕН. Парсинг не выполняется.")
    elif USE_FREELANCE == 1:
        logging.info("Режим парсинга: Kwork. Работаю по сервису Kwork.")
    elif USE_FREELANCE == 2:
        logging.info("Режим парсинга: Habr. Работаю по сервису Habr.")
    elif USE_FREELANCE == 3:
        logging.info("Режим парсинга: Kwork и Habr. Работаю по обоим сервисам.")


    while True:
        try:
            if USE_FREELANCE == 1 or USE_FREELANCE == 3:
                logging.info("Начинаем парсинг Kwork...")
                for page in range(1, 10 + 1):
                    new_kworks = get_kworks(category, page)
                    await save_to_database(new_kworks)
                logging.info("Парсинг Kwork завершен.")

            if USE_FREELANCE == 2 or USE_FREELANCE == 3:
                logging.info("Начинаем парсинг Habr...")
                await parser(ioloop)
                logging.info("Парсинг Habr завершен.")

            logging.info("Отдых 150 секунд...")
            await asyncio.sleep(150)
        except Exception as e:
            logging.error(f"Ошибка в основном цикле: {e}")

if __name__ == "__main__":
    asyncio.run(main())