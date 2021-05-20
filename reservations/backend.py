import datetime
import os
import urllib
from enum import Enum

import bs4
import requests_cache
from dateutil import rrule


class Backend:
    def __init__(self, base_url):
        self.base_url = base_url

        def login():
            session = requests_cache.CachedSession('sitzplatz-cache')
            session.proxies.update({
                'http': 'socks5h://127.0.0.1:9050',
                'https': 'socks5h://127.0.0.1:9050'
            })
            login_url = urllib.parse.urljoin(base=base_url, url='admin.php')
            # Get session cookie
            session.get(login_url)
            login_res = session.post(login_url,
                                          data={
                                              'NewUserName': os.environ.get('SP_USER'),
                                              'NewUserPassword': os.environ.get('SP_PASS'),
                                              'returl': self.base_url,
                                              'TargetURL': self.base_url,
                                              'Action': 'SetName'
                                          }, allow_redirects=False)
            if login_res.status_code == 200:
                print("Login failed!")
                print(login_res.text)
                exit(1)
            else:
                print('Logged in')
            return session

        self.session = login()

        def get_areas(session):
            r = session.get(self.base_url)
            b = bs4.BeautifulSoup(r.text, 'html.parser')

            area_div = b.find('div', id='dwm_areas')
            areas = []
            for li in area_div.find_all('li'):
                name = li.text.strip()
                url = urllib.parse.urlparse(li.a.get('href'))
                params = urllib.parse.parse_qs(url.query)
                number = ''.join(params['area'])
                areas.append({
                    'name': name,
                    'number': number
                })
            return areas

        self.areas = get_areas(self.session)

    def get_room_entries(self, date, area):
        url = urllib.parse.urljoin(base=self.base_url,
                                   url=f'day.php?year={date.year}&month={date.month}&day={date.day}&area={area}')
        r = self.session.get(url)
        b = bs4.BeautifulSoup(r.text, 'html.parser')

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
        for row in rows:
            row_entries = []
            col_index = 0
            row_label = 'N/A'
            for column in row.find_all('td'):
                classes = column.attrs["class"]
                if 'row_labels' in classes:
                    row_label = column.find(class_='celldiv').text.strip()
                    daytime = Daytime.MORNING if row_label == 'vormittags' else \
                        Daytime.AFTERNOON if row_label == 'nachmittags' else \
                            Daytime.EVENING

                    continue
                state = 'new' in classes and State.FREE or \
                        'private' in classes and State.OCCUPIED or \
                        'writable' in classes and State.MINE or \
                        State.UNKNOWN
                occupier = state in [State.FREE, State.MINE] and None or \
                           'I' in classes and 'internal' or \
                           'K' in classes and 'student' or \
                           'special'

                label = labels[col_index]
                row_entries.append({
                    'seat': label[0],
                    'room_id': label[1],
                    'state': state,
                    'occupier': occupier
                })
                col_index += 1
            times[daytime] = row_entries

        return times

    def get_day_entries(self, date):
        entries = {}
        for area in self.areas:
            room_entries = self.get_room_entries(date, area['number'])
            entries.update({
                area['name']: room_entries
            })
        return entries

    def search_bookings(self, start_day=datetime.datetime.today() + datetime.timedelta(days=1),
                        day_count=1,
                        state=None,
                        daytimes=None):
        bookings = []

        def time_bookings(time_entries, daytime):
            for seat in time_entries:
                if not state or seat["state"] == state:
                    bookings.append({
                        'date': date,
                        'daytime': daytime,
                        'seat': seat,
                        'room': room_name,
                    })

        for date in rrule.rrule(rrule.DAILY, count=day_count, dtstart=start_day):
            day_entries = self.get_day_entries(date)
            for room_name, room_entries in day_entries.items():
                if daytimes is None:
                    for time_name, time_entries in room_entries.items():
                        time_bookings(time_entries, time_name)
                else:
                    if isinstance(daytimes, Daytime):
                        daytimes = [daytimes]
                    for daytime in daytimes:
                        time_bookings(room_entries[daytime], daytime)

        return bookings


def daytime_to_name(daytime):
    if daytime == Daytime.MORNING:
        return 'Vormittags'
    elif daytime == Daytime.AFTERNOON:
        return 'Nachmittags'
    elif daytime == Daytime.EVENING:
        return 'Abends'
    else:
        raise AttributeError('Invalid daytime: {daytime}')

class State(Enum):
    FREE = 1
    OCCUPIED = 2
    MINE = 3
    UNKNOWN = 4


class Daytime(Enum):
    MORNING = 1
    AFTERNOON = 2
    EVENING = 3