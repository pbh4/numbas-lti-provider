from django.http import HttpResponse
from channels.handler import AsgiHandler
from channels import Group
from channels.sessions import channel_session
from channels.auth import http_session_user, channel_session_user, channel_session_user_from_http
from channels.generic import BaseConsumer
from channels.generic.websockets import WebsocketConsumer
import json
from datetime import datetime
from django.utils import timezone
from urllib.parse import parse_qs

from django.contrib.auth.models import User
from django.utils.translation import ugettext as _
from django_auth_lti.patch_reverse import reverse

from .groups import group_for_attempt
from .models import Attempt,ScormElement,Resource, ReportProcess,EditorLink
from .report_outcome import report_outcome, ReportOutcomeException
from .save_scorm_data import save_scorm_data

@channel_session_user_from_http
def ws_connect(message,pk):
    message.reply_channel.send({"accept": True})
    attempt = Attempt.objects.get(pk=pk)
    group = group_for_attempt(attempt)
    group.add(message.reply_channel)
    query = parse_qs(message.content['query_string'].decode('utf-8'))
    uid = query.get('uid',[''])[0]
    mode= query.get('mode',[''])[0]
    if mode!='review':
        group.send({'text': json.dumps({'current_uid': uid})})

@channel_session_user_from_http
def ws_disconnect(message,pk):
    attempt = Attempt.objects.get(pk=pk)
    group_for_attempt(attempt).discard(message.reply_channel)

@channel_session_user
def scorm_set_element(message,pk):
    packet = json.loads(message.content['text'])
    attempt = Attempt.objects.get(pk=pk)
    batches = {packet['id']: packet['data']}
    done, unsaved_elements = save_scorm_data(attempt,batches)
    response = {
        'received': done,
        'completion_status': attempt.completion_status,
        'unsaved_elements': unsaved_elements,
    }
    message.reply_channel.send({'text':json.dumps(response)})

def report_scores(message,**kwargs):
    resource = Resource.objects.get(pk=message['pk'])
    process = ReportProcess.objects.create(resource=resource)

    errors = []
    for user in User.objects.filter(attempts__resource=resource).distinct():
        try:
            request = report_outcome(resource,user)
        except ReportOutcomeException as e:
            errors.append(e)

    if len(errors):
        process.status = 'error'
        process.response = '\n'.join(e.message for e in errors)
    else:
        process.status = 'complete'
    process.save()

class AttemptScormListingConsumer(WebsocketConsumer):
    def connection_groups(self,pk,**kwargs):
        attempt = Attempt.objects.get(pk=pk)
        return [attempt.channels_group()]

def update_editorlink(message,**kwargs):
    editorlink = EditorLink.objects.get(pk=message['pk'])

    editorlink.update_cache(bounce=message.get('bounce',False))
    editorlink.save()
