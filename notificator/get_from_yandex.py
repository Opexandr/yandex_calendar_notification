import os
import requests
import base64
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
from icalendar import Calendar
from dateutil.rrule import rrulestr
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo
from icalendar.prop import vDatetime
from telegram import Bot
from telegram.error import TelegramError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
import logging
import asyncio

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()

# Конфигурация
email = os.environ["email"]
password = os.environ["calendar_password"]
telegram_token = os.environ["TELEGRAM_TOKEN"]
telegram_chat_id = os.environ["TELEGRAM_CHAT_ID"]
timezone = ZoneInfo("Europe/Moscow")

# Инициализация бота
bot = Bot(token=telegram_token)
scheduler = BackgroundScheduler(timezone=str(timezone))


class CalendarNotifier:
    def __init__(self):
        self.scheduled_jobs = {}
        self.last_scheduled_events = {}
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def get_caldav_events(self):
        """Получает события через CalDAV API"""
        url = f"https://caldav.yandex.ru/calendars/{email}/events-default/"
        headers = {
            "Authorization": f"Basic {base64.b64encode(f'{email}:{password}'.encode()).decode()}",
            "Content-Type": "application/xml",
        }
        body = """<?xml version="1.0"?>
        <C:calendar-query xmlns:C="urn:ietf:params:xml:ns:caldav">
            <D:prop xmlns:D="DAV:"><D:getetag/><C:calendar-data/></D:prop>
            <C:filter>
                <C:comp-filter name="VCALENDAR">
                    <C:comp-filter name="VEVENT"/>
                </C:comp-filter>
            </C:filter>
        </C:calendar-query>"""

        response = requests.request("REPORT", url, headers=headers, data=body, timeout=30)
        if response.status_code != 207:
            raise Exception(f"CalDAV error: {response.status_code}")

        return response.text

    def parse_events(self, xml_data):
        """Парсит события из XML ответа"""
        try:
            root = ET.fromstring(xml_data)
            events = []

            for calendar_data in root.findall(".//{urn:ietf:params:xml:ns:caldav}calendar-data"):
                if calendar_data.text:
                    try:
                        calendar = Calendar.from_ical(calendar_data.text)
                        for component in calendar.walk():
                            if component.name == "VEVENT":
                                events.append(component)
                    except Exception as e:
                        logger.error(f"Ошибка парсинга iCal: {str(e)[:100]}")
            return events
        except ET.ParseError as e:
            logger.error(f"Ошибка парсинга XML: {str(e)[:100]}")
            return []

    def normalize_datetime(self, dt, default_tz):
        """Нормализует дату/время к datetime с временной зоной"""
        if isinstance(dt, datetime):
            if dt.tzinfo is None:
                return dt.replace(tzinfo=default_tz)
            return dt
        elif isinstance(dt, date):
            return datetime.combine(dt, datetime.min.time()).replace(tzinfo=default_tz)
        return dt

    def expand_recurring_event(self, event, end_date):
        """Разворачивает повторяющееся событие"""
        try:
            dtstart = self.normalize_datetime(event.get('dtstart').dt, timezone)
            rrule = event.get('rrule')

            if not rrule:
                return [event]

            rrule_text = rrule.to_ical().decode('utf-8')
            full_rule = f"DTSTART;TZID={timezone.key}:{dtstart.strftime('%Y%m%dT%H%M%S')}\nRRULE:{rrule_text}"
            occurrences = list(rrulestr(full_rule, dtstart=dtstart))

            expanded = []
            for occurrence in occurrences:
                if isinstance(occurrence, datetime):
                    occurrence = self.normalize_datetime(occurrence, timezone)
                    occurrence_date = occurrence.date()
                else:
                    occurrence_date = occurrence
                    occurrence = self.normalize_datetime(occurrence, timezone)

                if occurrence_date > end_date.date():
                    continue

                new_event = event.copy()
                new_event['dtstart'] = vDatetime(occurrence)

                if 'dtend' in event:
                    end = self.normalize_datetime(event.get('dtend').dt, timezone)
                    duration = end - dtstart
                    new_event['dtend'] = vDatetime(occurrence + duration)

                expanded.append(new_event)

            return expanded if expanded else [event]
        except Exception as e:
            logger.error(f"Ошибка обработки повторяющегося события: {str(e)[:100]}")
            return [event]

    def get_upcoming_events(self, hours_ahead=24):
        """Получает предстоящие события на указанный период"""
        now = datetime.now(timezone)
        end_time = now + timedelta(hours=hours_ahead)

        xml_data = self.get_caldav_events()
        all_events = self.parse_events(xml_data)

        upcoming_events = []
        for event in all_events:
            expanded = self.expand_recurring_event(event, end_time)

            for ev in expanded:
                try:
                    start = self.normalize_datetime(ev.get('dtstart').dt, timezone)
                    end = self.normalize_datetime(
                        ev.get('dtend').dt if 'dtend' in ev else start,
                        timezone
                    )

                    if now <= start <= end_time:
                        upcoming_events.append({
                            'uid': str(ev.get('uid', '')),
                            'summary': str(ev.get('summary', 'Без названия')),
                            'start': start,
                            'end': end,
                            'recurring': 'rrule' in event
                        })
                except Exception as e:
                    logger.error(f"Ошибка обработки события: {str(e)[:100]}")

        return sorted(upcoming_events, key=lambda x: x['start'])

    async def async_send_notification(self, event, minutes_before=0):
        """Асинхронная отправка уведомления"""
        try:
            if minutes_before > 0:
                message = (f"🔔 Напоминание: через {minutes_before} минут начнётся встреча\n"
                           f"📌 {event['summary']}\n"
                           f"⏰ {event['start'].strftime('%H:%M')}-{event['end'].strftime('%H:%M')}")
            else:
                message = (f"🚀 Встреча начинается сейчас!\n"
                           f"📌 {event['summary']}\n"
                           f"⏰ {event['start'].strftime('%H:%M')}-{event['end'].strftime('%H:%M')}")

            await bot.send_message(chat_id=telegram_chat_id, text=message)
            logger.info(f"Уведомление отправлено: {event['summary']}")
        except TelegramError as e:
            logger.error(f"Ошибка отправки уведомления: {e}")

    def send_notification(self, event, minutes_before=0):
        """Синхронная обертка для отправки уведомления"""
        asyncio.run_coroutine_threadsafe(
            self.async_send_notification(event, minutes_before),
            self.loop
        ).result()

    async def async_send_scheduled_events_list(self):
        """Асинхронная отправка списка уведомлений"""
        try:
            if not self.last_scheduled_events:
                await bot.send_message(chat_id=telegram_chat_id, text="⏳ Нет запланированных уведомлений")
                return

            message = "📅 Запланированные уведомления:\n\n"
            for event_time, notifications in sorted(self.last_scheduled_events.items()):
                event = notifications[0]['event']
                message += (
                    f"📌 {event['summary']}\n"
                    f"⏰ {event['start'].strftime('%d.%m.%Y %H:%M')}-{event['end'].strftime('%H:%M')}\n"
                )

                for notification in notifications:
                    time_left = notification['trigger_time'] - datetime.now(timezone)
                    minutes = int(time_left.total_seconds() // 60)
                    message += f"   - {notification['type']} (через {minutes} мин)\n"

                message += "\n"

            await bot.send_message(chat_id=telegram_chat_id, text=message)
        except Exception as e:
            logger.error(f"Ошибка отправки списка уведомлений: {e}")

    def send_scheduled_events_list(self):
        """Синхронная обертка для отправки списка"""
        asyncio.run_coroutine_threadsafe(
            self.async_send_scheduled_events_list(),
            self.loop
        ).result()

    def schedule_notifications(self):
        """Планирует уведомления для предстоящих событий"""
        # Удаляем старые задания
        for job_id in list(self.scheduled_jobs.keys()):
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
            del self.scheduled_jobs[job_id]

        self.last_scheduled_events.clear()

        events = self.get_upcoming_events()

        for event in events:
            event_start = event['start']
            now = datetime.now(timezone)

            if event_start <= now:
                continue

            event_id = f"{event['uid']}_{event_start.timestamp()}"
            event_notifications = []

            # Уведомление за 10 минут
            if (event_start - timedelta(minutes=10)) > now:
                trigger_time = event_start - timedelta(minutes=10)
                scheduler.add_job(
                    self.send_notification,
                    trigger=DateTrigger(trigger_time),
                    args=[event, 10],
                    id=f"{event_id}_10m",
                    replace_existing=True
                )
                event_notifications.append({
                    'type': 'Напоминание за 10 мин',
                    'trigger_time': trigger_time,
                    'event': event
                })

            # Уведомление за 5 минут
            if (event_start - timedelta(minutes=5)) > now:
                trigger_time = event_start - timedelta(minutes=5)
                scheduler.add_job(
                    self.send_notification,
                    trigger=DateTrigger(trigger_time),
                    args=[event, 5],
                    id=f"{event_id}_5m",
                    replace_existing=True
                )
                event_notifications.append({
                    'type': 'Напоминание за 5 мин',
                    'trigger_time': trigger_time,
                    'event': event
                })

            # Уведомление о начале
            trigger_time = event_start
            scheduler.add_job(
                self.send_notification,
                trigger=DateTrigger(trigger_time),
                args=[event, 0],
                id=f"{event_id}_start",
                replace_existing=True
            )
            event_notifications.append({
                'type': 'Уведомление о начале',
                'trigger_time': trigger_time,
                'event': event
            })

            if event_notifications:
                self.last_scheduled_events[event_start.isoformat()] = event_notifications
                self.scheduled_jobs.update({
                    f"{event_id}_10m": True,
                    f"{event_id}_5m": True,
                    f"{event_id}_start": True
                })

        logger.info(f"Запланировано уведомлений: {len(self.scheduled_jobs)}")
        self.send_scheduled_events_list()

    def run(self):
        """Запускает планировщик уведомлений"""

        # Запускаем event loop в отдельном потоке
        def run_loop():
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()

        import threading
        threading.Thread(target=run_loop, daemon=True).start()

        scheduler.start()
        self.schedule_notifications()

        # Планируем регулярное обновление
        scheduler.add_job(
            self.schedule_notifications,
            'interval',
            minutes=30,
            id='update_schedule'
        )

        logger.info("Сервис уведомлений запущен")


if __name__ == "__main__":
    notifier = CalendarNotifier()
    notifier.run()

    try:
        while True:
            pass
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        notifier.loop.call_soon_threadsafe(notifier.loop.stop)
        logger.info("Сервис уведомлений остановлен")