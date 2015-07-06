from ..utils import register
from .. import format
from ..format import color
from datetime import timedelta, datetime, timezone

from aiohttp import request
import asyncio
import re

URL_API = 'http://api.openweathermap.org/data/2.5/forecast'

re_date = re.compile(r'([+-]?\d+)([dh])')

ICON_TO_UTF = {
    '01d': color('☀', color.yellow),
    '02d': color('☀', color.yellow),
    '03d': color('☁', color.white),
    '04d': color('☁', color.white),
    '09d': color('☔', color.light_blue),
    '10d': color('☔', color.light_blue),
    '11d': color('⚡', color.yellow),
    '13d': color('❄', color.white),
    '01n': color('☾', color.white),
    '02n': color('☾', color.white),
    '03n': color('☁', color.white),
    '04n': color('☁', color.white),
    '09n': color('☔', color.light_blue),
    '10n': color('☔', color.light_blue),
    '11n': color('⚡', color.yellow),
    '13n': color('❄', color.white),
}

@register(cmd='weather')
def weather(bot, city, day=None):
    dt = check_and_return_datetime(day)
    timestamp = dt.replace(tzinfo=timezone.utc).timestamp()

    def get_weather():
        response = yield from request('GET', URL_API, params={
            'q': city,
            'units': 'metric',
            'lang': 'pl'
        })
        data_list = (yield from response.json())["list"]
        key = lambda x: abs(x['dt'] - timestamp)
        data = sorted(data_list, key=key)[0]
        try:
            weather = data['weather'][0]
        except IndexError:
            weather = {'description': '', 'icon': ''}

        bot.reply('{city}: {temp} °C {icon} {desc}'.format(
            city=format.bold(city),
            temp=data['main']['temp'],
            desc=weather['description'],
            icon=ICON_TO_UTF.get(weather['icon'], '')
        ))

    asyncio.async(get_weather())


def check_and_return_datetime(day):
    dt = datetime.now()
    if day is None:
        return dt

    dt_hour = datetime.now().replace(hour=12, minute=0, second=0)
    if day == "yesterday":
        return dt_hour - timedelta(days=1)
    if day == "tomorrow":
        return dt_hour + timedelta(days=1)

    match = re_date.match(day)
    if match is None:
        return dt
    number = int(match.group(1))
    if match.group(2) == 'd':
        return dt_hour + timedelta(days=number)
    else:
        return dt + timedelta(hours=number)