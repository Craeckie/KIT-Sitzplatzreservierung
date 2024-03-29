import datetime
import json
import os
import pickle
import random
import re
import logging
import traceback
import urllib
from enum import IntEnum
from io import BytesIO
from urllib.parse import urljoin
from markdownify import markdownify as md

import bs4
import requests
from dateutil import rrule
from requests.cookies import RequestsCookieJar

from . import redis


class State(IntEnum):
    FREE = 1
    OCCUPIED = 2
    MINE = 3
    UNKNOWN = 4


class Backend:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.proxy = os.environ.get('PROXY')

        self.daytimes = self.get_daytimes()
        self.areas = self.get_areas()

    def get_areas(self) -> dict:
        redis_key = f'areas'
        areas_json = redis.get(redis_key)
        areas = json.loads(areas_json) if areas_json else None
        if not areas:
            print('Cache: reloading areas')
            r = self.get_request('/sitzplatzreservierung/')
            b = bs4.BeautifulSoup(r.text, 'lxml')

            area_div = b.find('div', id='dwm_areas')
            areas = {}
            for li in area_div.find_all('li'):
                name = li.text.strip()
                url = urllib.parse.urlparse(li.a.get('href'))
                params = urllib.parse.parse_qs(url.query)
                number = ''.join(params['area'])
                areas[number] = name
            redis.set(redis_key, json.dumps(areas), ex=24 * 3600)
        return areas

    def get_daytimes(self) -> list:
        redis_key = f'daytimes'
        daytimes_json = redis.get(redis_key)
        daytimes = json.loads(daytimes_json) if daytimes_json else None
        if not daytimes:
            print('Cache: reloading daytimes')
            r = self.get_request('/sitzplatzreservierung/')
            b = bs4.BeautifulSoup(r.text, 'lxml')

            table = b.find(id="day_main")

            rows = [r for r in table.tbody.children
                    if type(r) == bs4.element.Tag
                    and ('even_row' in r.attrs["class"] or 'odd_row' in r.attrs["class"])]
            daytimes = []
            index = 0
            for row in rows:
                link = row.div.a
                href = link.attrs['href']
                seconds_match = re.search('timetohighlight=(.*)$', href)
                seconds = seconds_match.group(1)
                name = link.text
                daytimes.append({
                    'name': name,
                    'seconds': seconds,
                    'index': index
                })
                index += 1
            redis.set(redis_key, json.dumps(daytimes), ex=24 * 3600)
        return daytimes

    def get_times(self) -> str:
        redis_key = f'times'
        times_data = redis.get(redis_key)
        times = times_data.decode('UTF-8') if times_data else None
        if not times:
            print('Cache: reloading times')
            r = self.get_request('/sitzplatzreservierung/')
            b = bs4.BeautifulSoup(r.text, 'lxml')

            try:
                time_div = b.find('div', id='hinweis')
                for tag in time_div.findAll('a'):
                    if 'title' in tag.attrs:
                        del tag.attrs['title']

                html = str(time_div).replace('*', '\\*') \
                                    .replace('(', '\\(') \
                                    .replace(')', '\\)')
                times = md(str(html), strip=['hr'],
                           strong_em_symbol='_').strip()
                text = ''
                parts = times.split('(')
                for p in parts:
                    if ')' in p:
                        inner_parts = p.split(')')
                        inner_parts[0] = markdown_strip_characters(inner_parts[0])
                        text += inner_parts[0] + ')'
                        text += ''.join([markdown_strip_characters(p) for p in inner_parts[1:]])
                        text += '('
                    else:
                        text += markdown_strip_characters(p) + '('
                text = text[:-1]
                times = text

                redis.set(redis_key, times.encode('UTF-8'), ex=24 * 3600)
            except Exception as e:
                with open('last-error-times.log', 'w') as f:
                    f.write(str(e) + '\n\n')
                    f.write(traceback.format_exc() + '\n\n')
                    f.write(str(b) + '\n\n')
                    f.write(str(r) + '\n')

        return times

    def login(self, user_id: str, user=None, password=None, captcha=None, cookies=None, login_required=False) \
            -> RequestsCookieJar|None:
        cookies_key = f'login-cookies:{user_id}'
        if not cookies:
            cookies_pickle = redis.get(cookies_key)
            cookies = pickle.loads(cookies_pickle) if cookies_pickle else None
        if cookies and not login_required:
            return cookies
        else:
            if not user or not password:
                res = self.get_request('admin.php', cookies=cookies)
                if 'Buchungsübersicht von' in res.text:
                    return res.cookies

            # Renew cookies using creds
            if not user or not password:
                creds = get_user_creds(user_id)
                if creds:
                    user = creds['user']
                    password = creds['password']
            if user and password and captcha:

                # Get the cookies
                data = {
                    'NewUserName': user.strip(),
                    'NewUserPassword': password,
                    'returl': self.base_url,
                    'TargetURL': self.base_url,
                    'Action': 'SetName',
                    'EULA': 'on',
                    'CaptchaText': captcha
                }
                login_res = self.post_request('admin.php',
                                              data=data,
                                              cookies=cookies,
                                              allow_redirects=False)
                if login_res.status_code == 200:
                    print(f'Login failed: {user}')
                    print(login_res.text)
                    data['NewUserPassword'] = '***REDACTED***'
                    print(data)
                    print("test")
                else:
                    # we need the library account number, even though login is possible using the Matrikelnummer
                    res = self.get_request('admin.php', cookies=login_res.cookies)
                    if 'Buchungsübersicht von' in res.text:
                        user_match = re.search('Buchungsübersicht von<br> ([0-9]+)</a>', res.text)
                        if user_match:
                            old_user = user
                            user = user_match.group(1)
                            print(f'Logged in {old_user} as {user}')
                            creds_json = {
                                'user': user,
                                'password': password
                            }
                            set_user_creds(user_id, creds_json)
                            redis.set(cookies_key, pickle.dumps(login_res.cookies))
                            return login_res.cookies
            return None

    def get_captcha(self) -> (BytesIO, RequestsCookieJar):
        res = self.get_request('admin.php')
        b = bs4.BeautifulSoup(res.text, 'lxml')
        captcha_div = b.find('div', attrs={'id': 'Captcha'})
        if not captcha_div:
            return None, None
        captcha_img = captcha_div.img
        if not captcha_img or 'src' not in captcha_img.attrs:
            return None, None
        url = captcha_img.attrs['src']
        url = self.get_absolute_url(url)
        res = self.get_request(url, cookies=res.cookies)
        # photo = BytesIO(res.content)
        # photo.seek(0)
        return res.content, res.cookies

    def get_room_entries(self, date: datetime.datetime, area, cookies: RequestsCookieJar = None) -> tuple[dict, bool]:
        url = get_day_url(date, area)

        cached = False
        times = {}
        redis_key = f'room_entries:{date.strftime("%y-%m-%d")}:{area}'
        if not cookies:
            cached_data = redis.get(redis_key)
            times_data = json.loads(cached_data) if cached_data else None
            if times_data:
                for daytime, entries in times_data.items():
                    for entry in entries:
                        entry['state'] = State(entry['state'])
                    times[int(daytime)] = entries
                    cached = True

        if not times:
            r = self.get_request(url, cookies=cookies)
            b = bs4.BeautifulSoup(r.text, 'lxml')

            try:
                table = b.find(id="day_main")

                labels = [(list(t.strings)[1], t.attrs['data-room'])
                          for t in list(table.thead.children)[1]
                          if type(t) == bs4.element.Tag
                          and 'data-room' in t.attrs]

                rows = [r for r in table.tbody.children
                        if type(r) == bs4.element.Tag
                        and ('even_row' in r.attrs["class"] or 'odd_row' in r.attrs["class"])]
                rows[0].td.find(class_='celldiv').text.strip()

                times = {}
                row_index = 0
                for row in rows:
                    row_entries = []
                    col_index = 0
                    row_label = 'N/A'
                    daytime = self.daytimes[0]
                    for column in row.find_all('td'):
                        classes = column.attrs["class"]
                        if 'row_labels' in classes:
                            row_label = column.find(class_='celldiv').text.strip()
                            # daytime = Daytime.MORNING if row_label == 'vormittags' else \
                            #     Daytime.AFTERNOON if row_label == 'nachmittags' else \
                            #         Daytime.EVENING
                            #daytime = self.daytimes[row_label]

                            continue
                        state = 'new' in classes and State.FREE or \
                                'private' in classes and State.OCCUPIED or \
                                'writable' in classes and State.MINE or \
                                State.UNKNOWN
                        occupier = state in [State.FREE, State.MINE] and None or \
                                   'I' in classes and 'Interne Buchungen' or \
                                   'K' in classes and 'KIT Studenten' or \
                                   'D' in classes and 'DHBW Studenten' or \
                                   'H' in classes and 'HsKa Studenten' or \
                                   'G' in classes and 'Private Buchungen' or \
                                   'P' in classes and 'Personal' or \
                                   'special'
                        div = column.div
                        entry_id = div.attrs['data-id'] if 'data-id' in div.attrs else None

                        label = labels[col_index]
                        row_entries.append({
                            'area': area,
                            'seat': label[0],
                            'room_id': label[1],
                            'state': state,
                            'occupier': occupier,
                            'entry_id': entry_id
                        })
                        col_index += 1
                    times[row_index] = row_entries
                    row_index += 1

                free_seats_min = min(len([entry for entry in entries if entry['state'] == State.FREE])
                                     for row_index, entries in times.items())
                total_seats = min(len([entry for entry in entries])
                                     for row_index, entries in times.items())

                # Adaptive expiry time for quick updates at important times
                expiry_time = 10 * 60
                now = datetime.datetime.now()
                if free_seats_min == 0:
                    expiry_time = 30
                elif free_seats_min < 5 and total_seats >= 10:
                    expiry_time = 10
                elif free_seats_min < 10 and total_seats >= 20:
                    expiry_time = 25
                elif free_seats_min < 15 and total_seats >= 30:
                    expiry_time = 2 * 60
                # Times when unused bookings are freed / new day comes
                elif date.date() == now.date() and now.hour in [23] + list(range(8, 19)):
                    minutes_to_next_half_hour = 30 - now.minute % 30
                    if minutes_to_next_half_hour == 0:
                        expiry_time = 60 - now.second
                    else:
                        expiry_time = min(5 * 60,  minutes_to_next_half_hour * 60)
                elif date.date() - now.date() >= datetime.timedelta(days=2):
                    expiry_time = 15 * 60
                expiry_time = max(0, expiry_time + random.randrange(-5, 5))
                logging.info(f'Cache: reloaded room entries on {date.date()} for {self.areas[area]}, expires in {expiry_time} seconds')
                redis.set(redis_key, json.dumps(times), ex=expiry_time)
            except Exception as e:
                with open('last-error-room-entries.log', 'w') as f:
                    f.write(str(e) + '\n\n')
                    f.write(traceback.format_exc() + '\n\n')
                    f.write(str(b) + '\n\n')
                    f.write(str(r) + '\n')

        return times, cached

    def get_day_entries(self, date: datetime.datetime, areas=None, cookies: RequestsCookieJar = None) -> dict:
        entries = {}
        areas = areas if areas else [a for a in self.areas.keys()]
        #ToDo: shuffle, but keep order on output
        #random.shuffle(areas)
        for area in areas if areas else [a for a in self.areas.keys()]:
            room_entries, cached = self.get_room_entries(date, area, cookies=cookies)
            entries.update({
                area: (room_entries, cached)
            })
        return entries

    def search_bookings(self, start_day: datetime.datetime = datetime.datetime.today() + datetime.timedelta(days=1),
                        day_count=1,
                        state=None,
                        daytimes=None,
                        areas: list = None,
                        cookies: RequestsCookieJar = None) -> list[dict]:
        bookings = []

        def time_bookings(time_entries: list, daytime, cached=False):
            for seat in time_entries:
                if not state or seat["state"] == state:
                    bookings.append({
                        'date': date,
                        'daytime': daytime,
                        'seat': seat,
                        'state': seat["state"],
                        'room': room_name,
                        'area': seat['area'],
                        'cached': cached
                    })

        for date in rrule.rrule(rrule.DAILY, count=day_count, dtstart=start_day):
            day_entries = self.get_day_entries(date, areas=areas, cookies=cookies)
            for room_name, (room_entries, cached) in day_entries.items():
                if daytimes is None:
                    for time_name, time_entries in room_entries.items():
                        time_bookings(time_entries, time_name, cached)
                else:
                    # if isinstance(daytimes, type(self.daytimes)):
                    #     daytimes = [daytimes]
                    # elif all(isinstance(d, int) for d in daytimes):
                    #     daytimes = [repr(self.daytimes(d)) for d in daytimes]
                    for daytime in daytimes:
                        if daytime < len(room_entries):
                            time_bookings(room_entries[daytime], daytime, cached)

        return bookings

    def book_seat(self, user_id, day_delta: int, daytime: int, room, seat, room_id, cookies: RequestsCookieJar) -> (bool, str):
        date = datetime.datetime.today() + datetime.timedelta(days=int(day_delta))
        creds = get_user_creds(user_id)
        user = creds['user']
        daytime = int(daytime)
        if 0 <= daytime < len(self.daytimes):
            seconds = self.daytimes[daytime]['seconds']
        else:
            raise AttributeError('Invalid daytime!')
        returl = self.get_absolute_url('day.php?area=20')
        returl += '&returl=' + urllib.parse.quote(returl, safe='')  # yes...
        daytime_str = self.daytimes[daytime]['name']
        data = {
            'name': user,
            'description': daytime_str.lower() + '+',
            'start_day': date.day,
            'start_month': date.month,
            'start_year': date.year,
            'start_seconds': str(seconds),
            'end_day': date.day,
            'end_month': date.month,
            'end_year': date.year,
            'end_seconds': str(seconds),
            'area': room,
            'rooms[]': room_id,
            'type': 'K',
            'confirmed': '1',
            'returl': returl,
            'create_by': user,
            'rep_id': '0',
            'edit_type': 'series'
        }
        data = {k: str(v) for k, v in data.items()}
        referer = self.get_absolute_url(get_day_url(date, room))
        # res = self.post_request(
        #             f'edit_entry.php?area={room}&room={room_id}&period={daytime}'
        #             f'&year={date.year}&month={date.month}&day={date.day}', cookies=cookies, data=data, referer=referer)
        res = self.post_request('edit_entry_handler.php', data={**data,
                                                                'ajax': '1'}, cookies=cookies, referer=referer)
        check_result = None
        try:
            check_result = res.json()
        except:
            pass
        res = self.post_request('edit_entry_handler.php', data=data, cookies=cookies, referer=referer, allow_redirects=False)
        if res.status_code == 302:
            msg = f"Erfolgreich gebucht!\nZeit: {date.strftime('%a, %d.%m')}, {daytime_str}\nOrt:  {self.areas[room]}, Platz {seat}"
            print(msg)
            return True, msg
        else:
            msg = check_result['rules_broken'][0] \
                if check_result and 'rules_broken' in check_result and check_result['rules_broken'] else None
            if not msg:
                page = bs4.BeautifulSoup(res.text, 'lxml')

                content = page.find(id="contents")
                msg = content.get_text() if content else None

            return False, msg

        # try:
        #     res = json.loads(res.text)
        #     if 'valid_booking' in res and res['valid_booking']:
        #         print(f"Erfolgreich gebucht: {data}")
        #         return True
        #     else:
        #         print(f"Buchen fehlgeschlagen: {data}")
        #         return False

        # except:
        #     print(f"Buchen fehlgeschlagen: {data}")
        #     return False

    def cancel_reservation(self, user_id, entry_id, cookies: RequestsCookieJar) -> (bool, str):
        creds = get_user_creds(user_id)
        user = creds['user']
        
        referer = self.get_absolute_url(f'view_entry.php?id={entry_id}&area=20&day=24&month=12&year=2021')

        now = datetime.datetime.now()
        url = ('del_entry.php?' +
               f'id={entry_id}&series=0&returl=report.php?'
               f'from_day={now.day}&from_month={now.month}&from_year={now.year}'
               f'&to_day=1&to_month=12&to_year=2030'
               f'&areamatch=&roommatch=&namematch=&descrmatch=&creatormatch={user}'
               f'&match_private=2&match_confirmed=2'
               f'&output=0&output_format=0&sortby=r&sumby=d&phase=2&datatable=1')
        res = self.get_request(url, referer=referer, cookies=cookies, allow_redirects=False)
        if res.status_code == 302:
            return True, None
        else:
            return False, None

    def get_reservations(self, user_id, cookies: RequestsCookieJar) -> list[dict]|None:
        creds = get_user_creds(user_id)
        user = creds['user']

        now = datetime.datetime.now()
        end = datetime.datetime(year=2030, month=12, day=1)
        res = self.get_request('report.php', cookies=cookies,
                               params={
                                   'from_day': now.day,
                                   'from_month': now.month,
                                   'from_year': now.year,
                                   'to_day': end.day,
                                   'to_month': end.month,
                                   'to_year': end.year,
                                   'areamatch': '',
                                   'roommatch': '',
                                   'namematch': '',
                                   'descrmatch': '',
                                   'creatormatch': user,
                                   'match_private': 2,
                                   'match_confirmed': 2,
                                   'output': 0,
                                   'output_format': 0,
                                   'sortby': 'd',
                                   'sumby': 'd',
                                   'datatable': 1,
                                   'phase': "2,2",
                                   'ajax': 1,
                                   '_': now.timestamp()
                               })
        if res.status_code != 200:
            return None

        data = json.loads(res.text)
        entries = []
        for j_entries in data['aaData']:
            entry = {}
            links = j_entries[0]
            b = bs4.BeautifulSoup(links, 'lxml')
            entry['id'] = b.a.attrs['data-id']
            entry['room'] = j_entries[1]
            entry['seat'] = j_entries[2]

            b = bs4.BeautifulSoup(j_entries[3], 'lxml')
            date = b.get_text().title()
            m = re.match('(?P<daytime>[A-Za-z]+), (?P<weekday>[A-Za-z]+) '
                     '(?P<day>[0-9]{2}) (?P<month>[A-Za-z]+) (?P<year>[0-9]{4})', date)
            if m:
                date = f"{m.group('weekday')}, {m.group('day')}. {m.group('month')}"
                entry['daytime'] = m.group('daytime')
            entry['date'] = date
            entries.append(entry)
        return entries

        # b = bs4.BeautifulSoup(res.text, 'lxml')
        # table = b.find(id="report_table")

        # rows = [r for r in table.tbody.children
        #         if type(r) == bs4.element.Tag]
        # entries = []
        # for row in rows:
        #     entry = {}
        #     columns = row.find_all('td')
        #     entry_links = columns[0].a
        #     entry['id'] = entry_links.attrs['data-id']
        #     entry['room'] = columns[1].get_text()
        #     entry['seat'] = columns[2].get_text()
        #     entry['date'] = columns[3].get_text()
        #     entries.append(entry)
        # return entries

    def get_request(self, *args, **kwargs):
        return self.request(*args, method='GET', **kwargs)

    def post_request(self, *args, **kwargs):
        return self.request(*args, method='POST', **kwargs)

    def request(self,
                suburl: str,
                method: str = 'GET',
                cookies: RequestsCookieJar = None,
                params: dict = None,
                referer: str = None,
                **kwargs):
        url = self.get_absolute_url(suburl)
        session = requests.session()
        if self.proxy:
            session.proxies.update({
                'http': self.proxy,
                'https': self.proxy
            })
        if cookies:
            session.cookies = cookies
        headers = {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'de_DE,en;q=0.5',
            'Connection': 'keep-alive',
            'Origin': self.base_url,
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; rv:78.0) Gecko/20100101 Firefox/78.0',
        }
        if referer:
            headers['referer'] = referer

        res = session.request(method=method, url=url, params=params, headers=headers, **kwargs)
        # Overwrite old cookies with new cookies
        session.cookies.update(res.cookies)
        res.cookies = session.cookies
        return res

    def get_absolute_url(self, suburl):
        return urljoin(self.base_url, suburl)


def get_day_url(date: datetime.datetime, area) -> str:
    return f'day.php?year={date.year}&month={date.month}&day={date.day}&area={area}'


def get_user_creds(user_id) -> dict:
    creds_key = f'login-creds:{user_id}'
    creds_json = redis.get(creds_key)
    creds = json.loads(creds_json) if creds_json else None
    return creds


def set_user_creds(user_id, data):
    creds_key = f'login-creds:{user_id}'
    redis.set(creds_key, json.dumps(data))


def remove_user_creds(user_id):
    creds_key = f'login-creds:{user_id}'
    redis.delete(creds_key)
    cookies_key = f'login-cookies:{user_id}'
    redis.delete(cookies_key)


def markdown_strip_characters(text):
    for char in ['-', '!', '.']:
        text = text.replace(char, f'\\{char}')
    return text
