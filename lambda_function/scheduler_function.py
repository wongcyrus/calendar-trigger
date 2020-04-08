import sys
from datetime import date, datetime, timedelta, timezone
import icalendar
from dateutil.rrule import *
import urllib.request
import pytz
import os
import boto3
import json

utc = pytz.UTC

dynamodb = boto3.resource('dynamodb')
sns = boto3.client('sns')
table = dynamodb.Table(os.environ['EventRecordsTable'])


def get_events_from_ics(ics_string, window_start, window_end):
    events = []

    def append_event(e):
        if e['startdt'] > window_end:
            return
        if e['enddt']:
            if e['enddt'] < window_start:
                return
        events.append(e)

    def get_recurrent_datetimes(recur_rule, start, exclusions):
        rules = rruleset()
        first_rule = rrulestr(recur_rule, dtstart=start)
        rules.rrule(first_rule)
        if not isinstance(exclusions, list):
            exclusions = [exclusions]

        for xdt in exclusions:
            try:
                rules.exdate(xdt.dt)
            except AttributeError:
                pass

        dates = []

        for d in rules.between(window_start, window_end):
            dates.append(d)
        return dates

    cal = filter(lambda c: c.name == 'VEVENT',
                 icalendar.Calendar.from_ical(ics_string).walk())

    def date_to_datetime(d):
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)

    for vevent in cal:
        summary = str(vevent.get('summary'))
        description = str(vevent.get('description'))
        location = str(vevent.get('location'))
        rawstartdt = vevent.get('dtstart').dt
        rawenddt = rawstartdt + timedelta(minutes=1)
        if 'dtend' in vevent:
            rawenddt = vevent.get('dtend').dt
        allday = False
        if not isinstance(rawstartdt, datetime):
            allday = True
            startdt = date_to_datetime(rawstartdt)
            if rawenddt:
                enddt = date_to_datetime(rawenddt)
        else:
            startdt = rawstartdt
            enddt = rawenddt

        exdate = vevent.get('exdate')
        if vevent.get('rrule'):
            reoccur = vevent.get('rrule').to_ical().decode('utf-8')
            if vevent.get('rrule').get('UNTIL') is None:
                continue
            end_time = vevent.get('rrule').get('UNTIL')[0]
            end_time = datetime(end_time.year, end_time.month, end_time.day)
            if end_time > datetime.now():
                for d in get_recurrent_datetimes(reoccur, startdt, exdate):
                    new_e = {
                        'startdt': d,
                        'allday': allday,
                        'summary': summary,
                        'desc': description,
                        'loc': location
                    }
                    if enddt:
                        new_e['enddt'] = d + (enddt - startdt)
                    append_event(new_e)
        else:
            append_event({
                'startdt': startdt,
                'enddt': enddt,
                'allday': allday,
                'summary': summary,
                'desc': description,
                'loc': location
            })
    events.sort(key=lambda e: e['startdt'])
    return events


def get_key(event):
    return '{} - {} - {} - {}'.format(event['startdt'], event['enddt'], event['summary'], event['desc'])
        

def is_duplicate(prefix, event):
    try:
        response = table.get_item(
            Key={
                'id': prefix + "-" + get_key(event)
            }
        )
    except ClientError as e:
        print(e.response['Error']['Message'])
        return False
    else:
        if 'Item' in response:
            item = response['Item']
            print("GetItem succeeded:")
            print(json.dumps(item, indent=4))
            return True
        else:
            return False


def save_event(prefix, event):
    response = table.put_item(
       Item={
            'id': prefix + "-" + get_key(event)
            }
    )    
    print("PutItem succeeded:")
    print(json.dumps(response, indent=4))


def publish_event(topicArn, subject, message):
    def default(o):
        if isinstance(o, (date, datetime)):
            return o.isoformat()
    message["Source"] = "Calendar-Trigger"        
    response = sns.publish(
        TopicArn=topicArn,
        Message= json.dumps(message, default=default),
        Subject= subject
    )
    print(response)

def lambda_handler(event, context):
    url = os.environ['CalendarUrl']

    with urllib.request.urlopen(url) as response:
        ics_string = response.read()

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=30)
    window_end = now + timedelta(minutes=30)
    events = get_events_from_ics(ics_string, window_start, window_end)

    for e in events:
        print(get_key(e))
        isDuplicateStartEvent = is_duplicate("Start", e)
        print(isDuplicateStartEvent)
        if not isDuplicateStartEvent:
            print("Publish Start Events.")
            save_event("Start", e)
            publish_event( os.environ['CanlenderEventStartTopic'], "Start " + e['summary'], e)
        else:
            print("Repeated Start event - " + get_key(e))
            
            
    window_start = now - timedelta(minutes=45)
    window_end = now + timedelta(minutes=30)
    past_events = get_events_from_ics(ics_string, window_start, window_end)

    ended_event_keys = list(set(map(get_key,past_events)) - set(map(get_key,events)))
    ended_events = [x for x in past_events if get_key(x) in ended_event_keys]
    
    print(ended_events)
    for e in ended_events:
        print("Publish Stop Events.")
        isDuplicateStopEvent = is_duplicate("Stop", e)
        print(isDuplicateStopEvent)
        if not isDuplicateStopEvent:
            print("Publish Stop Events.")
            save_event("Stop", e)
            publish_event( os.environ['CanlenderEventStopTopic'], "Stop" + e['summary'], e)
        else:
            print("Repeated Stop event - " + get_key(e))
        
